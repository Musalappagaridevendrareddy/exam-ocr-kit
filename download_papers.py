"""
Fetch the scanned exam papers straight from their official sources.

The repo carries only the list (papers.json), not the 8 GB of PDFs. Each entry
is a file the collection run already downloaded and checked on another machine,
with its official URL and sha256. Resumable: files already present are skipped.

    python download_papers.py            # everything in papers.json
    python download_papers.py --limit 3  # a quick check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "backend" / "data" / "raw_papers"
PART_DIR = ROOT / "backend" / "data" / "_part"
# Windows' own curl validates certificates against the Windows store and
# completes the partial chains many government sites serve.
CURL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "curl.exe")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PER_HOST_GAP = 1.0  # seconds between requests to one site


def safe_url(url: str) -> str:
    """curl rejects literal spaces; keep existing %xx escapes as they are."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, quote(p.path, safe="/%"), quote(p.query, safe="=&%"), ""))


def long_path(p: Path) -> str:
    """Windows refuses paths over 260 characters unless they carry the \\\\?\\ prefix."""
    s = str(p.resolve())
    return "\\\\?\\" + s if os.name == "nt" and not s.startswith("\\\\?\\") else s


def fetch(url: str, dest: Path) -> str | None:
    """Download one file. Returns None on success, else the reason."""
    os.makedirs(long_path(dest.parent), exist_ok=True)
    # Download under a short name first; some paper names are long enough to
    # push the full path past Windows' limit.
    PART_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PART_DIR / (hashlib.sha1(str(dest).encode()).hexdigest()[:16] + ".part")
    r = subprocess.run(
        [CURL, "-sS", "-L", "--fail", "--max-time", "180", "-A", UA, "-o", str(tmp), safe_url(url)],
        capture_output=True, text=True)
    if r.returncode != 0:
        # A few sites refuse Windows' TLS stack but accept Python's. Certificate
        # checking stays on here too.
        why = (r.stderr.strip() or f"curl exit {r.returncode}")[:160]
        try:
            req = urllib.request.Request(safe_url(url), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return f"{why}; then {type(e).__name__}: {str(e)[:100]}"
    with tmp.open("rb") as fh:
        head = fh.read(5)
    # Checked after the file is closed: Windows cannot delete an open file.
    if head != b"%PDF-":
        tmp.unlink(missing_ok=True)
        return "not a PDF (the site returned a web page)"
    os.replace(tmp, long_path(dest))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="stop after this many downloads")
    args = ap.parse_args()

    entries = json.loads((ROOT / "papers.json").read_text(encoding="utf-8"))
    todo = [e for e in entries if not os.path.exists(long_path(RAW / e["path"]))]
    print(f"{len(entries)} papers listed, {len(entries) - len(todo)} already here, {len(todo)} to fetch")
    if args.limit:
        todo = todo[: args.limit]

    last: dict[str, float] = {}
    ok, changed, failed = 0, 0, []
    for i, e in enumerate(todo, 1):
        host = urlsplit(e["url"]).netloc
        wait = PER_HOST_GAP - (time.time() - last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        try:
            reason = fetch(e["url"], RAW / e["path"])
        except Exception as err:  # one bad file must not stop a 2,000-file download
            reason = f"{type(err).__name__}: {str(err)[:120]}"
        last[host] = time.time()
        if reason:
            failed.append({"path": e["path"], "url": e["url"], "reason": reason})
            print(f"[{i}/{len(todo)}] FAIL {e['path']}: {reason}", flush=True)
            continue
        ok += 1
        with open(long_path(RAW / e["path"]), "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if e.get("sha256") and digest != e["sha256"]:
            changed += 1  # the site replaced the file since collection; still a PDF, so it is kept
        if i % 25 == 0 or i == len(todo):
            print(f"[{i}/{len(todo)}] {ok} fetched, {len(failed)} failed", flush=True)

    (ROOT / "download_report.json").write_text(
        json.dumps({"ok": ok, "changed_since_collection": changed, "failed": failed}, indent=1), encoding="utf-8")
    print(f"done: {ok} fetched, {changed} differ from the collected copy, {len(failed)} failed "
          f"(see download_report.json). Run again to retry the failures.")
    return 0


def _selfcheck() -> None:
    assert safe_url("https://a.in/x y/QP%20(1).pdf?a=1") == "https://a.in/x%20y/QP%20%281%29.pdf?a=1"


if __name__ == "__main__":
    _selfcheck()
    sys.exit(main())
