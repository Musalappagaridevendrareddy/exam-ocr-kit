"""
OCR the scanned papers with a local vision model (for the GPU box).

`ocr_papers.py` (Tesseract) reads the words fine but loses the reading order on
multi-column booklets. A vision model reads the page as a page. This script
sends each page image to any OpenAI-compatible local server (LM Studio,
llama.cpp `llama-server`) and writes the same sidecar `ocr_papers.py` writes:
`<name>.pdf.ocr.txt`, one page per form-feed, which `parse_questions.py`
already prefers over the PDF text layer. Nothing else changes.

The model only transcribes. It is never asked for an answer.

Usage (paths in the list are relative to backend/data/raw_papers/):
    # compare models on the sample: --tag keeps each model's output apart
    python scripts/ocr_vlm.py --list data/raw_papers/ocr_sample.txt --pages 1-5 \
        --model <model-id> --tag <short-name>
    # the real run, once a model is chosen (resumable: finished files are skipped)
    python scripts/ocr_vlm.py --list data/raw_papers/ocr_queue.txt --model <model-id>
"""

from __future__ import annotations

import argparse
import base64
import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import fitz  # PyMuPDF
import requests

BACKEND = Path(__file__).resolve().parent.parent
RAW_DIR = BACKEND / "data" / "raw_papers"
REPORT = RAW_DIR / "ocr_vlm_report.csv"
PAGE_BREAK = "\f"

PROMPT = (
    "Transcribe this exam paper page exactly as printed. "
    "Read in the order a candidate would: finish one column before the next. "
    "Put each question number with its question on a new line, and each option "
    "on its own line with its marker, for example (1) or (A). "
    "Keep every language in its own script. Do not translate, answer, explain, "
    "summarise or add anything. Write maths inline. Output plain text only."
)

# A bare "1)" could be either, so it counts as neither: this is only a proxy.
Q_LINE = re.compile(r"^\s*(?:Q\.?\s*\d{1,3}\b|\d{1,3}\s*\.)", re.M)
OPT_LINE = re.compile(r"^\s*(?:\([A-Da-d1-4]\)|[A-Da-d][).])\s+\S", re.M)


def page_numbers(spec: str | None, total: int) -> List[int]:
    if not spec:
        return list(range(total))
    first, _, last = spec.partition("-")
    return list(range(max(1, int(first)) - 1, min(total, int(last or first))))


def structure_counts(text: str) -> tuple[int, int]:
    """Cheap proxy for 'did the layout survive': question lines and option lines."""
    return len(Q_LINE.findall(text)), len(OPT_LINE.findall(text))


def ocr_page(png: bytes, args) -> str:
    r = requests.post(
        f"{args.api_url.rstrip('/')}/chat/completions",
        timeout=args.timeout,
        json={
            "model": args.model,
            "temperature": 0,
            "max_tokens": args.max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": args.prompt},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ]}],
        },
    )
    r.raise_for_status()
    choice = r.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        # A cut-off page silently loses its last questions; refuse it instead.
        tail = (choice["message"]["content"] or "")[-160:].replace("\n", " | ")
        raise ValueError(f"page hit max_tokens (model looping, or raise --max-tokens); it ended: ...{tail}")
    return (choice["message"]["content"] or "").replace(PAGE_BREAK, " ").strip()


def ocr_file(rel: str, args) -> dict | None:
    pdf = RAW_DIR / rel
    suffix = f".ocr.{args.tag}.txt" if args.tag else ".ocr.txt"
    out = pdf.with_suffix(pdf.suffix + suffix)
    if out.exists() and out.stat().st_size > 0 and not args.force:
        print(f"  have  {rel}")
        return None

    doc = fitz.open(pdf)
    try:
        pages = page_numbers(args.pages, doc.page_count)
        total, started = doc.page_count, time.time()
        pngs = [doc[n].get_pixmap(dpi=args.dpi).tobytes("png") for n in pages]
    finally:
        doc.close()

    def one(job):
        n, png = job
        text = ocr_page(png, args)
        print(f"    page {n + 1}/{total}: {len(text)} chars", flush=True)
        return text

    # map() keeps page order whatever order the server answers in.
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        texts = list(pool.map(one, zip(pages, pngs)))

    # Written only when every page succeeded: a partial sidecar would read as
    # the whole paper and hide the failure.
    text = PAGE_BREAK.join(texts)
    out.write_text(text, encoding="utf-8")
    q, o = structure_counts("\n".join(texts))
    return {
        "file": rel, "tag": args.tag or "", "model": args.model, "pages": len(pages),
        "sec_per_page": round((time.time() - started) / max(1, len(pages)), 1),
        "chars": len(text), "question_lines": q, "option_lines": o,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--list", help="text file, one PDF path per line")
    src.add_argument("--file", help="one PDF path")
    ap.add_argument("--model", required=True, help="model id as the local server names it")
    ap.add_argument("--tag", help="write <pdf>.ocr.<tag>.txt instead of the real sidecar (model comparison)")
    ap.add_argument("--pages", help="e.g. 1-5; needs --tag, a partial real sidecar is never written")
    ap.add_argument("--api-url", default="http://localhost:1234/v1")
    ap.add_argument("--dpi", type=int, default=150, help="raise to 200 if small print is misread")
    ap.add_argument("--max-tokens", type=int, default=6000)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--parallel", type=int, default=1,
                    help="pages sent to the server at once (the server needs as many slots)")
    ap.add_argument("--prompt", default=PROMPT, help="some OCR models want their own short prompt")
    ap.add_argument("--force", action="store_true", help="redo files that already have output")
    args = ap.parse_args()
    if args.pages and not args.tag:
        ap.error("--pages needs --tag")

    files = [args.file] if args.file else [
        ln.strip() for ln in Path(args.list).read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows, failed = [], 0
    for i, rel in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {rel}", flush=True)
        try:
            row = ocr_file(rel, args)
        except Exception as e:  # one bad file must not stop a two-day run
            failed += 1
            print(f"  FAIL  {rel}: {e}", flush=True)
            continue
        if row:
            rows.append(row)
            new = not REPORT.exists()
            with REPORT.open("a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(row))
                if new:
                    w.writeheader()
                w.writerow(row)
            print(f"  done  {row['pages']} pages, {row['sec_per_page']} s/page, "
                  f"{row['question_lines']} question lines, {row['option_lines']} option lines")

    pages = sum(r["pages"] for r in rows)
    if pages:
        secs = sum(r["sec_per_page"] * r["pages"] for r in rows)
        print(f"\n{len(rows)} files, {pages} pages, {secs / pages:.1f} s/page average, "
              f"{sum(r['question_lines'] for r in rows)} question lines, "
              f"{sum(r['option_lines'] for r in rows)} option lines, {failed} failed")
    print(f"report: {REPORT}")
    return 1 if failed else 0


def _selfcheck() -> None:
    q, o = structure_counts("1. What is X?\n(1) a\n(2) b\n(3) c\n(4) d\nQ.2 Next?\n(A) p\n(B) q\n")
    assert (q, o) == (2, 6), (q, o)
    assert page_numbers("2-3", 10) == [1, 2] and page_numbers(None, 2) == [0, 1]
    assert page_numbers("4-9", 5) == [3, 4]


if __name__ == "__main__":
    _selfcheck()
    sys.exit(main())
