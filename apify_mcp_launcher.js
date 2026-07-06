#!/usr/bin/env node
/*
 * apify_mcp_launcher.js — Apify 구글뉴스 MCP 런처 (다계정 자동전환 + 예산 자동설정)
 *
 * [동작]
 *   1) apify_api.txt 에서 토큰들을 위→아래 순서로 읽는다(맨 위 우선).
 *   2) 각 계정의 잔여 크레딧을 Apify /v2/users/me/limits 로 확인해, 잔여가 남은 '첫 계정'을 고른다.
 *      → 맨 위 계정이 월 $5 소진되면 자동으로 다음 계정으로 넘어간다(재시작 시 반영).
 *   3) apify_news_budget.json 의 daily_cap 을 per_account_daily(기본 30) × 키개수 로 자동 갱신.
 *   4) 고른 토큰을 APIFY_TOKEN 으로 주입하고 @apify/actors-mcp-server 를 실행(stdio 투명 통과).
 *
 * [모드] 인자 없이 = MCP 실행. `--check` = 계정별 잔여/선택만 출력하고 종료(MCP 미실행, 점검용).
 * [보안] 토큰 전체는 출력하지 않는다(접두 9자만 stderr). 결과/잔여 금액만 표시.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const DIR = __dirname;
const KEY_FILE = path.join(DIR, 'apify_api.txt');
const BUDGET_FILE = path.join(DIR, 'apify_news_budget.json');
const ACTOR = 'scrapeio/google-news-scraper';
const PLACEHOLDER = 'PASTE_YOUR_APIFY_TOKEN_HERE';
const PRICE_PER_ARTICLE = 0.005;     // $5 / 1000건
const MIN_REMAINING_USD = 0.10;      // 잔여가 이 이하면 '소진'으로 보고 다음 계정
const CHECK_ONLY = process.argv.includes('--check');

function err(msg) { process.stderr.write(msg + '\n'); }

function loadKeys() {
  let txt = '';
  try { txt = fs.readFileSync(KEY_FILE, 'utf8'); } catch (e) { return []; }
  const keys = [];
  for (let line of txt.split(/\r?\n/)) {
    line = line.trim();
    if (!line || line.startsWith('#')) continue;
    const tok = line.split('#')[0].trim();
    const label = line.includes('#') ? line.split('#').slice(1).join('#').trim() : '';
    if (tok && tok !== PLACEHOLDER) keys.push({ token: tok, label: label });
  }
  return keys;
}

async function remainingUsd(token) {
  // 잔여 크레딧(USD). 실패 시 null(확인 불가 → 후보에서 스킵).
  try {
    const r = await fetch('https://api.apify.com/v2/users/me/limits?token=' + encodeURIComponent(token));
    if (!r.ok) return null;
    const j = await r.json();
    const d = j.data || j;
    const cur = (d.current && typeof d.current.monthlyUsageUsd === 'number') ? d.current.monthlyUsageUsd : null;
    const max = (d.limits && typeof d.limits.maxMonthlyUsageUsd === 'number') ? d.limits.maxMonthlyUsageUsd : null;
    if (cur === null || max === null) return null;
    return Math.max(0, max - cur);
  } catch (e) { return null; }
}

function syncDailyCap(nKeys) {
  // daily_cap = per_account_daily × 키개수. used_today/date 는 건드리지 않음(코워크가 일일 리셋).
  try {
    const b = JSON.parse(fs.readFileSync(BUDGET_FILE, 'utf8'));
    const base = (typeof b.per_account_daily === 'number') ? b.per_account_daily : 30;
    b.daily_cap = base * Math.max(1, nKeys);
    fs.writeFileSync(BUDGET_FILE + '.tmp', JSON.stringify(b, null, 2));
    fs.renameSync(BUDGET_FILE + '.tmp', BUDGET_FILE);
    return b.daily_cap;
  } catch (e) { return null; }
}

(async () => {
  const keys = loadKeys();
  if (keys.length === 0) {
    err('[apify-launcher] apify_api.txt 에 유효한 토큰이 없습니다(자리표시자/빈 파일). 토큰을 넣어주세요.');
    process.exit(1);
  }

  const cap = syncDailyCap(keys.length);
  err('[apify-launcher] 키 ' + keys.length + '개 — daily_cap=' + cap + ' (per_account×키수)');

  let chosen = null, chosenIdx = -1;
  for (let i = 0; i < keys.length; i++) {
    const rem = await remainingUsd(keys[i].token);
    const lab = keys[i].label ? '(' + keys[i].label + ')' : '';
    if (rem === null) {
      err('  계정[' + i + ']' + lab + ' prefix=' + keys[i].token.slice(0, 9) + '*** 잔여 확인 실패 — 스킵');
      continue;
    }
    const arts = Math.floor(rem / PRICE_PER_ARTICLE);
    err('  계정[' + i + ']' + lab + ' prefix=' + keys[i].token.slice(0, 9) + '*** 잔여 ~$' + rem.toFixed(2) + ' (~' + arts + '건)');
    if (chosen === null && rem > MIN_REMAINING_USD) { chosen = keys[i]; chosenIdx = i; }
  }

  if (chosen === null) {
    chosen = keys[0]; chosenIdx = 0;
    err('[apify-launcher] 잔여 크레딧이 남은 계정을 못 찾음 — 맨 위 키로 시도(폴백).');
  }
  err('[apify-launcher] ▶ 활성 계정[' + chosenIdx + '] prefix=' + chosen.token.slice(0, 9) + '*** 선택');

  if (CHECK_ONLY) { process.exit(0); }

  const env = Object.assign({}, process.env, { APIFY_TOKEN: chosen.token });
  const child = spawn('npx', ['-y', '@apify/actors-mcp-server', '--actors', ACTOR],
                      { stdio: 'inherit', env: env, shell: true });
  child.on('exit', (code, signal) => process.exit(code === null ? (signal ? 1 : 0) : code));
  child.on('error', (e) => { err('[apify-launcher] npx 실행 실패: ' + e.message); process.exit(1); });
})();
