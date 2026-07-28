# -*- coding: utf-8 -*-
"""
taildrop_receive.py — Taildrop 수신 → 검증 → 세션 폴더 이관 자동화 [v11.0 신규]

[왜] 노트북이 `/batch {"send":true}` 로 캡처 묶음을 보내면 **다운로드 폴더**에
  `kairos_<날짜시각>.zip` 으로 떨어진다. 그대로 두면 (a) 분석이 못 찾고 (b) 다운로드가 지저분해지고
  (c) 어느 zip 을 이미 처리했는지 알 수 없다. 이 스크립트가 받기→검증→이관→표시를 한 번에 한다.

[동작]
  1) `tailscale file get <다운로드>` — 노트북이 보낸 파일을 실제로 내려받는다(대기 중인 것만).
  2) 다운로드 폴더에서 `kairos_*.zip` 을 찾는다(**다른 파일은 절대 건드리지 않는다**).
  3) zip 안 `manifest.json` 으로 **sha256·marker_text 검증** 후 PNG 만 세션에 푼다.
  4) 처리한 zip 은 `_taildrop_done/` 으로 옮긴다(재처리 방지 — 삭제하지 않는다).
  5) 세션에 `hts_capture_batch.json` 기록(무엇이 들어왔고 무엇이 폐기됐는지).

[★검증] hts_capture_collect 와 같은 계약이다:
  · manifest 의 sha256 != 실제 PNG 해시 → 그 파일 폐기(전송 손상)
  · marker_text 에 기대 화면번호가 없음 → 폐기(엉뚱한 화면)
  · 검증 실패는 조용히 넘기지 않고 rejected 에 사유와 함께 남긴다.

[★안전] zip 내부 경로에 `..` 이나 절대경로가 있으면 그 항목을 거부한다(zip slip 방지).
  PNG·manifest.json 외 확장자는 풀지 않는다.

[사용법]
  python taildrop_receive.py                  # 받기 + 오늘 세션으로 이관
  python taildrop_receive.py --no-fetch       # 이미 받아둔 zip 만 처리
  python taildrop_receive.py --session <경로> --downloads <경로>
  python taildrop_receive.py --check          # 대기 중인 zip 목록만 표시
"""
import os
import re
import sys
import json
import shutil
import hashlib
import zipfile
import argparse
import logging
import subprocess
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
DEFAULT_DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
DONE_DIRNAME = "_taildrop_done"
ZIP_PAT = re.compile(r"^kairos_.*\.zip$", re.I)

# tailscale CLI — PATH 에 없을 수 있어 기본 설치 경로를 폴백으로 둔다
TS_CANDIDATES = (
    "tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
)

logging.basicConfig(level=logging.INFO, format="[taildrop] %(message)s")
log = logging.getLogger("taildrop")

from common import save_json_atomic, resolve_session


def _ts_exe():
    for c in TS_CANDIDATES:
        if os.path.sep in c:
            if os.path.isfile(c):
                return c
        elif shutil.which(c):
            return shutil.which(c)
    return None


def fetch(downloads=DEFAULT_DOWNLOADS, timeout=120):
    """대기 중인 Taildrop 파일을 내려받는다 → (받은 개수 추정, 메시지)."""
    exe = _ts_exe()
    if not exe:
        return 0, "tailscale CLI 를 찾을 수 없음"
    try:
        os.makedirs(downloads, exist_ok=True)
    except Exception as e:
        return 0, "다운로드 폴더 생성 실패: %s" % type(e).__name__
    before = set(os.listdir(downloads))
    try:
        # -conflict=rename: 같은 이름이 있어도 덮어쓰지 않는다(사용자 파일 보호)
        r = subprocess.run([exe, "file", "get", "-conflict=rename", downloads],
                           capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
    except Exception as e:
        return 0, "실행 실패: %s" % type(e).__name__
    after = set(os.listdir(downloads))
    got = len(after - before)
    return got, (out or err or "대기 파일 없음")


def _safe_member(name):
    """zip slip 방지 + 허용 확장자만."""
    n = name.replace("\\", "/")
    if n.startswith("/") or ".." in n.split("/"):
        return None
    base = os.path.basename(n)
    if not base:
        return None
    ext = os.path.splitext(base)[1].lower()
    if ext not in (".png", ".json"):
        return None
    return base


def process_zip(zip_path, img_dir, expect_screens=None):
    """zip 1개 → (files[], rejected[], manifest). 검증 통과한 PNG 만 푼다."""
    files, rejected, manifest = [], [], {}
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception as e:
        return [], [{"file": os.path.basename(zip_path),
                     "reason": "zip 열기 실패: %s" % type(e).__name__}], {}
    with zf:
        names = zf.namelist()
        # manifest 먼저
        for n in names:
            if os.path.basename(n).lower() == "manifest.json":
                try:
                    manifest = json.loads(zf.read(n).decode("utf-8", "replace"))
                except Exception:
                    manifest = {}
                break
        by_name = {}
        for m in (manifest.get("files") or []):
            if isinstance(m, dict) and m.get("file"):
                by_name[os.path.basename(str(m["file"]))] = m

        for n in names:
            base = _safe_member(n)
            if not base or base.lower() == "manifest.json":
                if base is None:
                    rejected.append({"file": n, "reason": "허용되지 않는 경로/확장자(zip slip 방지)"})
                continue
            try:
                data = zf.read(n)
            except Exception as e:
                rejected.append({"file": base, "reason": "읽기 실패: %s" % type(e).__name__})
                continue
            meta = by_name.get(base, {})
            # ★sha256 검증(manifest 에 있을 때만 — 없으면 그 사실을 남긴다)
            want = str(meta.get("sha256") or "").lower()
            got = hashlib.sha256(data).hexdigest()
            if want and want != got:
                rejected.append({"file": base, "reason": "sha256 불일치(전송 손상)"})
                continue
            if not data.startswith(b"\x89PNG"):
                rejected.append({"file": base, "reason": "PNG 아님"})
                continue
            marker = str(meta.get("marker_text") or "")
            # 파일명 앞 4자리가 화면번호인 컨벤션(0231_short_lend.png)이면 marker 와 대조
            m_no = re.match(r"^(\d{4})_", base)
            if m_no and marker and ("[%s]" % m_no.group(1)) not in marker:
                rejected.append({"file": base,
                                 "reason": "marker 불일치 — 기대 [%s], 실제 %r"
                                           % (m_no.group(1), marker[:40])})
                continue
            if meta.get("settled") is False:
                rejected.append({"file": base, "reason": "settled=false(갱신 중 캡처)"})
                continue
            try:
                with open(os.path.join(img_dir, base), "wb") as f:
                    f.write(data)
            except Exception as e:
                rejected.append({"file": base, "reason": "저장 실패: %s" % type(e).__name__})
                continue
            files.append({"file": base, "bytes": len(data),
                          "marker_text": marker or None,
                          "variant": meta.get("variant"),
                          "sha256_verified": bool(want),
                          "image": "hts_captures/" + base})
    return files, rejected, manifest


def main():
    ap = argparse.ArgumentParser(description="Taildrop 수신 → 세션 이관")
    ap.add_argument("--session", default=None)
    ap.add_argument("--downloads", default=DEFAULT_DOWNLOADS)
    ap.add_argument("--no-fetch", action="store_true", help="받기 생략(이미 있는 zip 만 처리)")
    ap.add_argument("--check", action="store_true", help="대기 zip 목록만 표시")
    args = ap.parse_args()

    dl = args.downloads
    if not args.no_fetch and not args.check:
        n, msg = fetch(dl)
        log.info("수신: %d개 (%s)", n, msg[:80])

    try:
        zips = sorted([f for f in os.listdir(dl) if ZIP_PAT.match(f)])
    except Exception as e:
        log.warning("다운로드 폴더 접근 실패: %s", type(e).__name__)
        return 0
    if not zips:
        log.info("처리할 kairos_*.zip 없음 (다운로드: %s)", dl)
        return 0
    if args.check:
        for z in zips:
            p = os.path.join(dl, z)
            log.info("대기: %s (%.1f KB)", z, os.path.getsize(p) / 1024)
        return 0

    session = args.session or resolve_session(OUTPUT_DIR)
    if not session or not os.path.isdir(session):
        log.warning("세션 폴더 없음 → 이관 보류(zip 은 그대로 둔다)")
        return 0
    img_dir = os.path.join(session, "hts_captures")
    os.makedirs(img_dir, exist_ok=True)
    done_dir = os.path.join(dl, DONE_DIRNAME)
    os.makedirs(done_dir, exist_ok=True)

    all_files, all_rej, srcs = [], [], []
    for z in zips:
        zp = os.path.join(dl, z)
        files, rej, manifest = process_zip(zp, img_dir)
        all_files += files
        all_rej += rej
        srcs.append({"zip": z, "n_ok": len(files), "n_rejected": len(rej),
                     "sent_at_kst": manifest.get("sent_at_kst"),
                     "capture": manifest.get("capture")})
        log.info("%s → 통과 %d · 폐기 %d", z, len(files), len(rej))
        for r in rej:
            log.warning("   폐기 %s — %s", r["file"], r["reason"][:60])
        # ★성공 여부와 무관하게 재처리 방지를 위해 옮긴다(삭제하지 않음 — 원본 보존)
        try:
            shutil.move(zp, os.path.join(done_dir, z))
        except Exception as e:
            log.warning("   이동 실패(다음 실행에서 재처리됨): %s", type(e).__name__)

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tz": "KST",
        "source": "taildrop_batch", "downloads_dir": dl,
        "n_zip": len(srcs), "n_ok": len(all_files), "n_rejected": len(all_rej),
        "zips": srcs, "files": all_files, "rejected": all_rej,
        "note": ("Taildrop 으로 받은 배치 캡처. rejected 는 sha256·marker·settled 검증에 걸려 "
                 "폐기된 장이며 그 사유가 곧 신뢰도 정보다. 처리한 zip 은 다운로드/"
                 + DONE_DIRNAME + " 로 옮겨 재처리를 막는다(삭제하지 않음)."),
    }
    out = os.path.join(session, "hts_capture_batch.json")
    try:
        save_json_atomic(out, payload)
        log.info("저장: %s (통과 %d · 폐기 %d)", out, len(all_files), len(all_rej))
    except Exception as e:
        log.warning("저장 실패: %s", type(e).__name__)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("예기치 못한 오류(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
