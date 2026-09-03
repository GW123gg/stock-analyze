#!/usr/bin/env node
'use strict';

/*
 * terminal-mcp — a zero-dependency MCP server for running shell commands
 * and analyzing log files. Speaks the MCP stdio protocol (newline-delimited
 * JSON-RPC 2.0) directly, so it runs on Claude Desktop's bundled Node runtime
 * with no npm install required.
 *
 * Commands run as background JOBS:
 *   - run_command   waits up to `wait_ms` (default 90s). If the command is
 *                   still running, it returns a job_id instead of killing it.
 *   - wait_job      keeps waiting on a running job.
 *   - cancel_job    stops a running job (force=false ~Ctrl-C / force=true kill).
 *   - list_jobs     lists running / recently-finished jobs.
 *   - read_file     reads a text/log file with tail / head / grep.
 *   - list_directory lists a directory.
 *
 * Optional safety policy (for unattended / auto-approved use): set a policy
 * file via config; it can block all commands, deny command patterns, restrict
 * allowed working directories, or whitelist allowed command patterns.
 */

const { spawn, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { StringDecoder } = require('string_decoder');

const SERVER_NAME = 'terminal-mcp';
const SERVER_VERSION = '2.0.0';
const DEFAULT_PROTOCOL = '2025-06-18';
const isWin = process.platform === 'win32';

const MAX_OUTPUT = 1000000;   // chars returned per stream (1 MB, was 200 KB)
const STORE_CAP = 4000000;    // chars retained in memory per stream per job
const MAX_JOBS = 50;          // retained job records before eviction
const WAIT_MIN = 1000;
const WAIT_MAX = 600000;      // max single wait chunk (10 min); re-call to wait more

// --- configuration (injected via env from the .mcpb user_config) -----------
// Returns '' when the value is missing or still an unsubstituted "${...}".
function cfg(name) {
  const v = process.env[name];
  if (v === undefined || v === null) return '';
  const t = String(v).trim();
  if (!t || t.indexOf('${') !== -1) return '';
  return t;
}

const DEFAULT_CWD = cfg('TERMINAL_MCP_DEFAULT_CWD') || os.homedir();
const DEFAULT_SHELL = (cfg('TERMINAL_MCP_DEFAULT_SHELL') ||
  (isWin ? 'powershell' : 'bash')).toLowerCase();
const DEFAULT_WAIT = parseInt(cfg('TERMINAL_MCP_DEFAULT_WAIT_MS'), 10) || 90000;
const HARD_KILL = (function () {
  const raw = cfg('TERMINAL_MCP_HARD_KILL_MS');
  if (raw === '') return 1800000;            // 30 min backstop by default
  const n = parseInt(raw, 10);
  return isNaN(n) ? 1800000 : n;             // 0 = never auto-kill
})();
const POLICY_FILE = cfg('TERMINAL_MCP_POLICY_FILE');

// --- low-level IO -----------------------------------------------------------
function log() {
  try { process.stderr.write('[terminal-mcp] ' + Array.prototype.join.call(arguments, ' ') + '\n'); }
  catch (e) { /* ignore */ }
}
function send(msg) {
  try { process.stdout.write(JSON.stringify(msg) + '\n'); }
  catch (e) { log('write error', e && e.message); }
}
function sendResult(id, result) { send({ jsonrpc: '2.0', id: id, result: result }); }
function sendError(id, code, message) { send({ jsonrpc: '2.0', id: id, error: { code: code, message: message } }); }

function truncate(text) {
  if (text.length > MAX_OUTPUT) {
    return text.slice(0, MAX_OUTPUT) + '\n\n... [truncated at ' + MAX_OUTPUT +
      ' characters; ' + (text.length - MAX_OUTPUT) + ' more omitted]';
  }
  return text;
}

// When PowerShell's stderr is a pipe it serializes the error/warning streams
// as CLIXML; recover plain text. No-op for non-CLIXML input.
function decodePsString(t) {
  return t
    .replace(/_x([0-9A-Fa-f]{4})_/g, function (_, h) { return String.fromCharCode(parseInt(h, 16)); })
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&#x([0-9A-Fa-f]+);/g, function (_, h) { return String.fromCharCode(parseInt(h, 16)); })
    .replace(/&#(\d+);/g, function (_, d) { return String.fromCharCode(parseInt(d, 10)); })
    .replace(/&amp;/g, '&');
}
function deClixml(s) {
  if (!s || s.indexOf('#< CLIXML') === -1) return s;
  const xml = s.slice(s.indexOf('#< CLIXML') + 9);
  const parts = [];
  const re = /<S\s+S="[^"]*">([\s\S]*?)<\/S>/g;
  let m;
  while ((m = re.exec(xml)) !== null) parts.push(decodePsString(m[1]));
  const out = parts.length ? parts.join('') : decodePsString(xml.replace(/<[^>]+>/g, ''));
  return out.replace(/\s+$/, '');
}

// --- safety policy ----------------------------------------------------------
function loadPolicy() {
  if (!POLICY_FILE) return null;
  try { return JSON.parse(fs.readFileSync(POLICY_FILE, 'utf8')); }
  catch (e) { log('policy read error:', e.message); return null; }
}
// Returns a string reason if the command is blocked, or null if allowed.
function checkPolicy(command, cwd) {
  const p = loadPolicy();
  if (!p) return null;
  if (p.block === true) return 'all commands are blocked by policy (block=true)';
  const denies = p.deny_patterns || p.deny || [];
  for (let i = 0; i < denies.length; i++) {
    try { if (new RegExp(denies[i], 'i').test(command)) return 'matched policy deny rule /' + denies[i] + '/'; }
    catch (e) { /* skip bad regex */ }
  }
  const allows = p.allow_patterns || p.allow_only || [];
  if (allows.length) {
    let ok = false;
    for (let i = 0; i < allows.length; i++) {
      try { if (new RegExp(allows[i], 'i').test(command)) { ok = true; break; } } catch (e) { /* skip */ }
    }
    if (!ok) return 'command does not match any policy allow_patterns';
  }
  const dirs = p.allowed_cwds || [];
  if (dirs.length) {
    const norm = path.resolve(cwd).toLowerCase();
    let ok = false;
    for (let i = 0; i < dirs.length; i++) {
      if (norm.indexOf(path.resolve(dirs[i]).toLowerCase()) === 0) { ok = true; break; }
    }
    if (!ok) return 'cwd "' + cwd + '" is not within policy allowed_cwds';
  }
  return null;
}

// --- job registry -----------------------------------------------------------
const jobs = new Map();
let jobCounter = 0;

function evictOld() {
  if (jobs.size <= MAX_JOBS) return;
  const done = [];
  for (const j of jobs.values()) if (j.done) done.push(j);
  done.sort(function (a, b) { return (a.finishedAt || 0) - (b.finishedAt || 0); });
  while (jobs.size > MAX_JOBS && done.length) jobs.delete(done.shift().id);
}

function resolveShell(shell) {
  const s = (shell || DEFAULT_SHELL || 'powershell').toLowerCase();
  if (s === 'cmd') return 'cmd';
  if (s === 'bash') return 'bash';
  return 'powershell';
}

function appendCapped(job, key, s) {
  job[key] += s;
  if (job[key].length > STORE_CAP) {
    job[key] = job[key].slice(job[key].length - STORE_CAP);
    job.storeTruncated = true;
  }
}

// Starts a job. Returns { job } or { error: '...' }.
function startJob(args) {
  const command = args && args.command;
  if (!command || !String(command).trim()) return { error: '"command" is required.' };
  const cwd = (args.cwd && String(args.cwd).trim()) || DEFAULT_CWD;
  const shell = resolveShell(args.shell);

  const reason = checkPolicy(String(command), cwd);
  if (reason) return { error: 'Blocked by safety policy: ' + reason + '.' };

  let file, spawnArgs;
  if (shell === 'cmd') {
    file = process.env.ComSpec || 'cmd.exe';
    spawnArgs = ['/d', '/c', 'chcp 65001>nul & ' + command];
  } else if (shell === 'bash') {
    file = 'bash';
    spawnArgs = ['-c', command];
  } else {
    file = 'powershell.exe';
    const prelude =
      '$ProgressPreference="SilentlyContinue"; $ErrorActionPreference="Continue"; ' +
      '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; ';
    const encoded = Buffer.from(prelude + command, 'utf16le').toString('base64');
    spawnArgs = ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded];
  }

  let child;
  try { child = spawn(file, spawnArgs, { cwd: cwd, windowsHide: true, env: process.env }); }
  catch (e) { return { error: 'Failed to start ' + shell + ': ' + e.message }; }

  jobCounter += 1;
  const job = {
    id: 'job-' + jobCounter,
    command: String(command), shell: shell, cwd: cwd, child: child,
    stdout: '', stderr: '', done: false, code: null, signal: null,
    canceled: false, cancelForce: false, hardKilled: false,
    startedAt: Date.now(), finishedAt: null, storeTruncated: false,
    _waiters: [], _outDec: new StringDecoder('utf8'), _errDec: new StringDecoder('utf8')
  };
  jobs.set(job.id, job);
  evictOld();

  child.stdout.on('data', function (d) { appendCapped(job, 'stdout', job._outDec.write(d)); });
  child.stderr.on('data', function (d) { appendCapped(job, 'stderr', job._errDec.write(d)); });

  function finish() {
    if (job.done) return;
    job.done = true;
    job.finishedAt = Date.now();
    appendCapped(job, 'stdout', job._outDec.end());
    appendCapped(job, 'stderr', job._errDec.end());
    if (job.hardTimer) { clearTimeout(job.hardTimer); job.hardTimer = null; }
    const waiters = job._waiters.slice();
    job._waiters.length = 0;
    waiters.forEach(function (w) { try { w(); } catch (e) { /* ignore */ } });
  }
  child.on('error', function (e) { appendCapped(job, 'stderr', '\n[spawn error: ' + e.message + ']'); job.code = -1; finish(); });
  child.on('close', function (code, signal) { job.code = code; job.signal = signal; finish(); });

  if (HARD_KILL > 0) {
    job.hardTimer = setTimeout(function () { job.hardKilled = true; doCancel(job, true); }, HARD_KILL);
  }
  return { job: job };
}

function doCancel(job, force) {
  if (job.done) return;
  job.canceled = true;
  job.cancelForce = !!force;
  try {
    if (isWin && job.child && job.child.pid) {
      const flags = force ? '/T /F' : '/T';
      try { execSync('taskkill /pid ' + job.child.pid + ' ' + flags, { stdio: 'ignore' }); } catch (e) { /* ignore */ }
    }
    job.child.kill(force ? 'SIGKILL' : 'SIGTERM');
  } catch (e) { /* ignore */ }
}

function clampWait(ms, fallback) {
  let w = parseInt(ms, 10);
  if (!w || w <= 0) w = fallback;
  if (w < WAIT_MIN) w = WAIT_MIN;
  if (w > WAIT_MAX) w = WAIT_MAX;
  return w;
}

// Resolves when the job finishes or `waitMs` elapses. Emits progress pings if
// the client supplied a progressToken, which also keeps the request alive.
function waitForJob(job, waitMs, progressToken) {
  return new Promise(function (resolve) {
    if (job.done) return resolve();
    let settled = false;
    const started = Date.now();
    let progressTimer = null;
    function cleanup() {
      if (timer) clearTimeout(timer);
      if (progressTimer) clearInterval(progressTimer);
      const i = job._waiters.indexOf(onDone);
      if (i >= 0) job._waiters.splice(i, 1);
    }
    function onDone() { if (settled) return; settled = true; cleanup(); resolve(); }
    const timer = setTimeout(onDone, waitMs);
    job._waiters.push(onDone);
    if (progressToken !== undefined && progressToken !== null) {
      progressTimer = setInterval(function () {
        send({ jsonrpc: '2.0', method: 'notifications/progress', params: {
          progressToken: progressToken,
          progress: Math.round((Date.now() - started) / 1000),
          message: job.id + ' running (' + Math.round((Date.now() - job.startedAt) / 1000) + 's)'
        } });
      }, 10000);
    }
  });
}

function jobText(job) {
  const elapsed = Math.round(((job.finishedAt || Date.now()) - job.startedAt) / 1000);
  let status;
  if (!job.done) status = 'RUNNING (' + elapsed + 's elapsed)';
  else if (job.hardKilled) status = 'TERMINATED by hard cap after ' + elapsed + 's';
  else if (job.canceled) status = 'CANCELED' + (job.cancelForce ? ' (force-killed)' : ' (interrupted)') + ' after ' + elapsed + 's';
  else status = 'exit ' + job.code + (job.signal ? ' (' + job.signal + ')' : '') + ' in ' + elapsed + 's';

  let out = '$ ' + job.command + '\n[shell: ' + job.shell + ' | cwd: ' + job.cwd + ' | ' + job.id + ' | ' + status + ']';
  if (job.storeTruncated) out += '\n[note: output exceeded buffer; showing the most recent portion]';
  const so = truncate(job.stdout.replace(/\s+$/, ''));
  const se = truncate(deClixml(job.stderr).replace(/\s+$/, ''));
  if (so) out += '\n--- stdout ---\n' + so;
  if (se) out += '\n--- stderr ---\n' + se;
  if (!so && !se) out += '\n(no output' + (job.done ? '' : ' yet') + ')';
  if (!job.done) {
    out += '\n\n[STILL RUNNING — not an error. The command exceeded the wait window. ' +
      'Decide based on the task: call wait_job("' + job.id + '") to keep waiting, ' +
      'or cancel_job("' + job.id + '") to stop it (force=false interrupts like Ctrl-C, force=true force-kills the process tree).]';
  }
  return out;
}

// --- tool definitions -------------------------------------------------------
const TOOLS = [
  {
    name: 'run_command',
    description: 'Run a shell command on this machine. Waits up to wait_ms ' +
      '(default ' + DEFAULT_WAIT + 'ms) for it to finish. If it finishes, returns ' +
      'stdout/stderr/exit code. If it is still running after the wait, it is NOT ' +
      'killed — you get a job_id and must then call wait_job (keep waiting) or ' +
      'cancel_job (stop it). On Windows the default shell is PowerShell.',
    inputSchema: {
      type: 'object',
      properties: {
        command: { type: 'string', description: 'The command line to run, exactly as typed in the shell.' },
        cwd: { type: 'string', description: 'Working directory. Defaults to the configured default directory.' },
        shell: { type: 'string', enum: ['powershell', 'cmd', 'bash'], description: 'Which shell to use. Defaults to powershell on Windows.' },
        wait_ms: { type: 'number', description: 'How long to wait before returning a still-running job_id. Default ' + DEFAULT_WAIT + ', max ' + WAIT_MAX + '.' }
      },
      required: ['command']
    }
  },
  {
    name: 'wait_job',
    description: 'Keep waiting on a background command started by run_command. ' +
      'Returns the final result if it finishes within wait_ms, otherwise the ' +
      'updated running status so you can decide again.',
    inputSchema: {
      type: 'object',
      properties: {
        job_id: { type: 'string', description: 'The job_id returned by run_command.' },
        wait_ms: { type: 'number', description: 'How long to wait this time. Default ' + DEFAULT_WAIT + ', max ' + WAIT_MAX + '.' }
      },
      required: ['job_id']
    }
  },
  {
    name: 'cancel_job',
    description: 'Stop a running command. force=false sends an interrupt (like ' +
      'Ctrl-C / graceful terminate); force=true force-kills the whole process tree.',
    inputSchema: {
      type: 'object',
      properties: {
        job_id: { type: 'string', description: 'The job_id to cancel.' },
        force: { type: 'boolean', description: 'false = interrupt (default), true = force-kill the process tree.' }
      },
      required: ['job_id']
    }
  },
  {
    name: 'list_jobs',
    description: 'List currently running and recently finished commands with their job_id, status and elapsed time.',
    inputSchema: { type: 'object', properties: {} }
  },
  {
    name: 'read_file',
    description: 'Read a text file (e.g. a log) from disk. Supports tail (last N ' +
      'lines), head (first N lines) and grep (keep only lines matching a ' +
      'case-insensitive regex) — handy for log analysis.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Absolute or relative path to the file.' },
        tail_lines: { type: 'number', description: 'If set, return only the last N lines.' },
        head_lines: { type: 'number', description: 'If set, return only the first N lines.' },
        grep: { type: 'string', description: 'If set, keep only lines matching this case-insensitive regular expression.' }
      },
      required: ['path']
    }
  },
  {
    name: 'list_directory',
    description: 'List the entries of a directory with type, size and last-modified time.',
    inputSchema: {
      type: 'object',
      properties: { path: { type: 'string', description: 'Directory path. Defaults to the configured default directory.' } }
    }
  }
];

// --- tools: jobs ------------------------------------------------------------
function toolRunCommand(args, progressToken) {
  const started = startJob(args);
  if (started.error) return Promise.resolve({ text: 'Error: ' + started.error, isError: true });
  const job = started.job;
  const waitMs = clampWait(args.wait_ms, DEFAULT_WAIT);
  return waitForJob(job, waitMs, progressToken).then(function () {
    const text = jobText(job);
    if (job.done) jobs.delete(job.id);
    return { text: text, isError: false };
  });
}

function toolWaitJob(args, progressToken) {
  const job = jobs.get(args && args.job_id);
  if (!job) return Promise.resolve({ text: 'Error: no such job "' + (args && args.job_id) + '" (it may have finished and been cleared).', isError: true });
  if (job.done) { const t = jobText(job); jobs.delete(job.id); return Promise.resolve({ text: t, isError: false }); }
  const waitMs = clampWait(args.wait_ms, DEFAULT_WAIT);
  return waitForJob(job, waitMs, progressToken).then(function () {
    const text = jobText(job);
    if (job.done) jobs.delete(job.id);
    return { text: text, isError: false };
  });
}

function toolCancelJob(args) {
  const job = jobs.get(args && args.job_id);
  if (!job) return Promise.resolve({ text: 'Error: no such job "' + (args && args.job_id) + '".', isError: true });
  if (job.done) { const t = jobText(job); jobs.delete(job.id); return Promise.resolve({ text: 'Already finished.\n' + t, isError: false }); }
  doCancel(job, !!(args && args.force));
  return waitForJob(job, 4000, null).then(function () {
    const text = jobText(job);
    if (job.done) { jobs.delete(job.id); return { text: text, isError: false }; }
    return { text: text + '\n\n[did not stop within 4s; call cancel_job with force=true to force-kill]', isError: false };
  });
}

function toolListJobs() {
  if (!jobs.size) return { text: 'No active or recent jobs.', isError: false };
  const rows = [];
  jobs.forEach(function (job) {
    const elapsed = Math.round(((job.finishedAt || Date.now()) - job.startedAt) / 1000);
    let st = job.done ? (job.canceled ? 'canceled' : (job.hardKilled ? 'hard-killed' : 'exit ' + job.code)) : 'running';
    rows.push(job.id + '  [' + st + ', ' + elapsed + 's]  ' + job.command.slice(0, 100));
  });
  return { text: rows.join('\n'), isError: false };
}

// --- tools: files -----------------------------------------------------------
function toolReadFile(args) {
  const p = args && args.path;
  if (!p || !String(p).trim()) return { text: 'Error: "path" is required.', isError: true };
  let resolved = String(p);
  if (!path.isAbsolute(resolved)) resolved = path.resolve(DEFAULT_CWD, resolved);
  let content;
  try { content = fs.readFileSync(resolved, 'utf8'); }
  catch (e) { return { text: 'Error reading file: ' + e.message, isError: true }; }

  let lines = content.split(/\r?\n/);
  let note = 'File: ' + resolved + ' (' + lines.length + ' lines)';
  if (args.grep) {
    let re;
    try { re = new RegExp(String(args.grep), 'i'); }
    catch (e) { return { text: 'Invalid grep regex: ' + e.message, isError: true }; }
    lines = lines.filter(function (l) { return re.test(l); });
    note += ' | grep /' + args.grep + '/i -> ' + lines.length + ' matches';
  }
  if (args.head_lines && args.head_lines > 0) { lines = lines.slice(0, args.head_lines); note += ' | head ' + args.head_lines; }
  if (args.tail_lines && args.tail_lines > 0) { lines = lines.slice(-args.tail_lines); note += ' | tail ' + args.tail_lines; }
  return { text: truncate(note + '\n\n' + lines.join('\n')), isError: false };
}

function toolListDirectory(args) {
  const p = (args && args.path && String(args.path).trim()) || DEFAULT_CWD;
  const resolved = path.isAbsolute(p) ? p : path.resolve(DEFAULT_CWD, p);
  let entries;
  try { entries = fs.readdirSync(resolved, { withFileTypes: true }); }
  catch (e) { return { text: 'Error listing directory: ' + e.message, isError: true }; }
  const rows = entries.map(function (e) {
    const type = e.isDirectory() ? 'DIR ' : (e.isSymbolicLink() ? 'LINK' : 'FILE');
    let size = '', mtime = '';
    try {
      const st = fs.statSync(path.join(resolved, e.name));
      size = e.isDirectory() ? '' : String(st.size);
      mtime = st.mtime.toISOString().replace('T', ' ').slice(0, 19);
    } catch (err) { /* ignore */ }
    return type + '  ' + mtime + '  ' + size.padStart(12) + '  ' + e.name;
  });
  return { text: truncate('Directory: ' + resolved + ' (' + entries.length + ' entries)\n\n' + rows.join('\n')), isError: false };
}

// --- request dispatch -------------------------------------------------------
function handleToolCall(id, params) {
  const name = params && params.name;
  const args = (params && params.arguments) || {};
  const progressToken = params && params._meta && params._meta.progressToken;
  let p;
  if (name === 'run_command') p = toolRunCommand(args, progressToken);
  else if (name === 'wait_job') p = toolWaitJob(args, progressToken);
  else if (name === 'cancel_job') p = toolCancelJob(args);
  else if (name === 'list_jobs') p = Promise.resolve(toolListJobs());
  else if (name === 'read_file') p = Promise.resolve(toolReadFile(args));
  else if (name === 'list_directory') p = Promise.resolve(toolListDirectory(args));
  else { sendError(id, -32602, 'Unknown tool: ' + name); return; }

  p.then(function (res) {
    sendResult(id, { content: [{ type: 'text', text: res.text }], isError: !!res.isError });
  }).catch(function (e) {
    sendResult(id, { content: [{ type: 'text', text: 'Tool execution failed: ' + (e && e.message ? e.message : String(e)) }], isError: true });
  });
}

function handleMessage(msg) {
  if (!msg || msg.jsonrpc !== '2.0') return;
  const id = msg.id, method = msg.method, params = msg.params;
  if (method === 'initialize') {
    const clientProto = params && params.protocolVersion;
    sendResult(id, {
      protocolVersion: clientProto || DEFAULT_PROTOCOL,
      capabilities: { tools: {} },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION }
    });
    return;
  }
  if (typeof method === 'string' && method.indexOf('notifications/') === 0) return;
  if (method === 'initialized') return;
  if (method === 'tools/list') { sendResult(id, { tools: TOOLS }); return; }
  if (method === 'tools/call') { handleToolCall(id, params); return; }
  if (method === 'ping') { sendResult(id, {}); return; }
  if (method === 'resources/list') { sendResult(id, { resources: [] }); return; }
  if (method === 'prompts/list') { sendResult(id, { prompts: [] }); return; }
  if (id === undefined || id === null) return;
  sendError(id, -32601, 'Method not found: ' + method);
}

// --- lifecycle --------------------------------------------------------------
function killAllJobs() {
  jobs.forEach(function (job) {
    if (!job.done && job.child && job.child.pid) {
      try {
        if (isWin) { try { execSync('taskkill /pid ' + job.child.pid + ' /T /F', { stdio: 'ignore' }); } catch (e) { /* ignore */ } }
        job.child.kill('SIGKILL');
      } catch (e) { /* ignore */ }
    }
  });
}
process.on('exit', killAllJobs);
process.on('SIGTERM', function () { killAllJobs(); process.exit(0); });
process.on('SIGINT', function () { killAllJobs(); process.exit(0); });
process.stdout.on('error', function () { /* ignore EPIPE */ });

let buffer = '';
const stdinDec = new StringDecoder('utf8');
process.stdin.on('data', function (chunk) {
  buffer += stdinDec.write(chunk);
  let idx;
  while ((idx = buffer.indexOf('\n')) >= 0) {
    let line = buffer.slice(0, idx);
    buffer = buffer.slice(idx + 1);
    line = line.replace(/\r$/, '').trim();
    if (!line) continue;
    let parsed;
    try { parsed = JSON.parse(line); }
    catch (e) { log('JSON parse error:', e.message); continue; }
    try { if (Array.isArray(parsed)) parsed.forEach(handleMessage); else handleMessage(parsed); }
    catch (e) { log('handler error:', e.message); }
  }
});
process.stdin.on('end', function () { killAllJobs(); process.exit(0); });
process.stdin.resume();

log('started v' + SERVER_VERSION + ' (cwd=' + DEFAULT_CWD + ', shell=' + DEFAULT_SHELL +
  ', wait=' + DEFAULT_WAIT + 'ms, hardKill=' + (HARD_KILL || 'off') +
  ', policy=' + (POLICY_FILE || 'none') + ')');
