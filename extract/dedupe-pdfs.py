#!/usr/bin/env python3
"""Strip consecutive duplicate pages from the issue PDFs.

The early-era scan PDFs pad each printed page into a short run of
byte-identical copies (issue 1955-5: 100 PDF pages, only 42 real; it
tapers off and is gone by the mid-1970s). This drops any page whose
embedded scan image is identical to the previous kept page, in place,
after copying the original to downloads/_pdf_orig/.

For every slimmed issue it writes downloads/spiegel-<y>-<n>/pagemap.json
(`kept[new_page - 1]` = the original PDF page number) and renumbers any
OCR text already in pdf-pages/ to match.

    .venv/bin/python3 extract/dedupe-pdfs.py [--from Y] [--to Y] [--issue Y-N] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spiegel_config import DATA_DIR  # noqa: E402

DOWNLOAD_DIR = DATA_DIR
BACKUP_DIR = DOWNLOAD_DIR / "_pdf_orig"


def page_count(pdf_path):
    out = subprocess.run(["pdfinfo", str(pdf_path)], check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":")[1].strip())
    return 0


def page_image_hashes(pdf_path, tmp_dir):
    """{page_num: sha1(embedded scan image bytes)} - best effort, {} on trouble."""
    try:
        rows = subprocess.run(["pdfimages", "-list", str(pdf_path)], check=True,
                              capture_output=True, text=True).stdout.splitlines()[2:]
        page_by_index = {}
        for row in rows:
            parts = row.split()
            if len(parts) > 1 and parts[0].isdigit() and parts[1].isdigit():
                page_by_index[int(parts[1])] = int(parts[0])
        subprocess.run(["pdfimages", "-all", str(pdf_path), str(tmp_dir / "img")],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, ValueError, IndexError):
        return {}

    by_page = {}
    for image_file in tmp_dir.glob("img-*"):
        if image_file.suffix == ".params":
            continue
        try:
            index = int(image_file.stem.rsplit("-", 1)[1])
        except ValueError:
            continue
        page = page_by_index.get(index)
        if page is not None:
            by_page.setdefault(page, hashlib.sha1(image_file.read_bytes()).hexdigest())
    return by_page


def pages_to_keep(hashes, total):
    """Original page numbers to keep: drop a page identical to the previous kept one."""
    keep, last = [], object()
    for page in range(1, total + 1):
        digest = hashes.get(page)
        if digest is not None and digest == last:
            continue
        keep.append(page)
        last = digest
    return keep


def slim_pdf(src, dst, keep):
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdfseparate", str(src), f"{tmp}/p-%d.pdf"], check=True, capture_output=True)
        parts = [f"{tmp}/p-{p}.pdf" for p in keep]
        subprocess.run(["pdfunite", *parts, str(dst)], check=True, capture_output=True)


def remap_ocr(issue_dir, keep):
    """Renumber existing pdf-pages/page-N.txt to the slimmed page numbering."""
    pages_dir = issue_dir / "pdf-pages"
    if not pages_dir.is_dir():
        return 0
    staged = {}
    for new_page, old_page in enumerate(keep, 1):
        src = pages_dir / f"page-{old_page}.txt"
        if src.exists():
            tmp = pages_dir / f"_remap-{new_page}.txt"
            src.rename(tmp)
            staged[new_page] = tmp
    for leftover in pages_dir.glob("page-*.txt"):
        leftover.unlink()
    for new_page, tmp in staged.items():
        tmp.rename(pages_dir / f"page-{new_page}.txt")
    return len(staged)


def issue_key(pdf_path):
    year, issue = pdf_path.stem.split("-")[1:]
    return int(year), int(issue)


def process(pdf_path, dry_run):
    year, issue = issue_key(pdf_path)
    backup = BACKUP_DIR / pdf_path.name
    issue_dir = DOWNLOAD_DIR / f"spiegel-{year}-{issue}"

    if backup.exists():
        return ("skip-done", 0, 0)

    try:
        total = page_count(pdf_path)
    except subprocess.CalledProcessError:
        return ("unreadable-pdf", 0, 0)

    with tempfile.TemporaryDirectory() as tmp:
        keep = pages_to_keep(page_image_hashes(pdf_path, Path(tmp)), total)

    if len(keep) == total:
        return ("no-dupes", total, total)
    if dry_run:
        return ("would-slim", total, len(keep))

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf_path, backup)
    slim_tmp = pdf_path.with_suffix(".slim.pdf")
    try:
        slim_pdf(backup, slim_tmp, keep)
        if page_count(slim_tmp) != len(keep):
            raise RuntimeError(f"slim page count {page_count(slim_tmp)} != {len(keep)}")
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        slim_tmp.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)  # leave original untouched
        return (f"failed: {exc}", total, total)

    slim_tmp.replace(pdf_path)
    (issue_dir).mkdir(parents=True, exist_ok=True)
    (issue_dir / "pagemap.json").write_text(
        json.dumps({"n_before": total, "n_after": len(keep), "kept": keep}), encoding="utf-8")
    remapped = remap_ocr(issue_dir, keep)
    return ("slimmed", total, len(keep), remapped)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_year", type=int, default=0)
    parser.add_argument("--to", dest="to_year", type=int, default=9999)
    parser.add_argument("--issue", help="only this issue, e.g. 1955-5")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pdfs = sorted(DOWNLOAD_DIR.glob("spiegel-*.pdf"), key=issue_key)
    if args.issue:
        y, i = map(int, args.issue.split("-"))
        pdfs = [p for p in pdfs if issue_key(p) == (y, i)]
    else:
        pdfs = [p for p in pdfs if args.from_year <= issue_key(p)[0] <= args.to_year]

    counts = {}
    pages_before = pages_after = 0
    for pdf in pdfs:
        result = process(pdf, args.dry_run)
        status = result[0]
        counts[status] = counts.get(status, 0) + 1
        if len(result) >= 3:
            pages_before += result[1]
            pages_after += result[2]
        if status in ("slimmed", "would-slim") or status.startswith("failed"):
            extra = f"  (+{result[3]} OCR pages remapped)" if len(result) > 3 else ""
            print(f"{pdf.stem}: {result[1]} -> {result[2]} pages  [{status}]{extra}", flush=True)

    print(f"\n{len(pdfs)} issues: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if pages_before:
        print(f"pages: {pages_before} -> {pages_after}  ({pages_before - pages_after} dropped)")


if __name__ == "__main__":
    main()
