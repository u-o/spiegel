#!/usr/bin/env python3
"""OCR every downloaded Spiegel issue PDF - one text file per page.

The magazine PDFs are page scans with no text layer, so each page is
rendered to an image and run through tesseract (German). Output lands in

    downloads/spiegel-<year>-<issue>/pdf-pages/page-<n>.txt

Pages already written are skipped, so the run is fully resumable. A page
whose embedded scan image is byte-identical to one already processed
(recurring full-page ads, boilerplate) reuses the cached OCR text instead
of running tesseract again - worth roughly a third of the corpus.

The first time an issue is seen it is also run through the same slimming
pass as dedupe-pdfs.py: the early-era scans pad each printed page into a
run of byte-identical copies, so those dupes are stripped from the PDF in
place (original backed up to downloads/_pdf_orig/, pagemap.json written)
before OCR, keeping page numbers canonical. Once _pdf_orig/<name> exists
this is skipped. Pass --no-dedupe to turn it off.

Work is parallelised across issues; tesseract itself is pinned to one
thread (OMP_THREAD_LIMIT=1) so the worker pool controls core usage.

By default the run pauses whenever the laptop is on battery and resumes
when it's plugged back in, so it won't drain the battery while travelling
(pass --run-on-battery to disable that).

    .venv/bin/python3 extract/ocr-pdfs.py [--workers N] [--from YEAR] [--to YEAR]
                                  [--dpi N] [--run-on-battery]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spiegel_config import DATA_DIR  # noqa: E402

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # one thread per tesseract; pool does the rest

# Gate that workers wait on; a monitor thread clears it while on battery.
POWER_OK = threading.Event()
POWER_OK.set()

DOWNLOAD_DIR = DATA_DIR
HASH_CACHE_DIR = DOWNLOAD_DIR / "_ocr_hash_cache"
PDF_BACKUP_DIR = DOWNLOAD_DIR / "_pdf_orig"
OCR_LANG = "deu"
DEFAULT_DPI = 300

DEHYPHEN_RE = re.compile(r"-\s*\n\s*")

# From 1994 on the PDFs are born-digital with a real text layer; a page whose
# layer has at least this many non-space chars is taken as-is, anything less
# (photo pages, image-only ads) still goes through tesseract.
MIN_TEXT_LAYER_CHARS = 30

Result = namedtuple("Result", "name total new errors note")


def _run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def on_ac_power():
    """True if the Mac is on wall power (or if power state can't be read)."""
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "AC Power" in out


def power_monitor(poll_seconds=20):
    """Clear POWER_OK while on battery, set it again once back on AC power."""
    while True:
        ac = on_ac_power()
        if ac and not POWER_OK.is_set():
            print(f"[{time.strftime('%H:%M:%S')}] on AC power - resuming OCR", flush=True)
            POWER_OK.set()
        elif not ac and POWER_OK.is_set():
            print(f"[{time.strftime('%H:%M:%S')}] on battery - pausing OCR until plugged in", flush=True)
            POWER_OK.clear()
        time.sleep(poll_seconds)


def page_count(pdf_path):
    for line in _run(["pdfinfo", str(pdf_path)]).splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":")[1].strip())
    return 0


def text_layer_pages(pdf_path):
    """{page_num: text} from the PDF's own text layer, or {} for a pure scan.

    An issue counts as born-digital if its text layer holds real text at all;
    the scan-era PDFs carry only a few stray bytes."""
    try:
        out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf_path), "-"],
                             check=True, capture_output=True).stdout.decode("utf-8", "replace")
    except subprocess.CalledProcessError:
        return {}
    pages = out.split("\f")
    if sum(len(p.strip()) for p in pages) < 2000:
        return {}
    return {n: text for n, text in enumerate(pages, 1)}


def page_image_hashes(pdf_path, tmp_dir):
    """{page_num: sha1(embedded scan image bytes)} - best effort, {} on trouble.

    Hashes the raw image bytes lifted straight out of the PDF (no
    rendering), so identical pages across the archive can share one OCR.
    """
    try:
        rows = _run(["pdfimages", "-list", str(pdf_path)]).splitlines()[2:]
        page_by_index = {}
        for row in rows:
            parts = row.split()
            if len(parts) > 1 and parts[0].isdigit() and parts[1].isdigit():
                page_by_index[int(parts[1])] = int(parts[0])
        subprocess.run(
            ["pdfimages", "-all", str(pdf_path), str(tmp_dir / "img")],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, ValueError, IndexError):
        return {}

    hashes = {}
    for image_file in tmp_dir.glob("img-*"):
        if image_file.suffix == ".params":
            continue  # sidecar metadata, not image data
        try:
            index = int(image_file.stem.rsplit("-", 1)[1])
        except ValueError:
            continue
        page = page_by_index.get(index)
        if page is not None:
            hashes[page] = hashlib.sha1(image_file.read_bytes()).hexdigest()
    return hashes


def pages_to_keep(hashes, total):
    """Original page numbers to keep: drop a page whose scan image is
    identical to the previous kept one (the early-era PDFs pad each printed
    page into a run of byte-identical copies)."""
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


def remap_ocr_pages(pages_dir, keep):
    """Renumber any existing pdf-pages/page-N.txt to the slimmed numbering."""
    if not pages_dir.is_dir():
        return
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


def dedupe_issue(pdf_path, hashes, total, out_dir):
    """Slim scan-padding dupes out of a freshly downloaded issue PDF, in place.

    Backs the original up to _pdf_orig/, writes pagemap.json, renumbers any
    OCR text already written for it, and returns (new_total, new_hashes)
    remapped to the slimmed page numbers. A no-op once _pdf_orig/<name>
    exists (the bulk dedupe-pdfs.py run, or an earlier OCR pass, did it),
    and it leaves the original untouched if anything goes wrong.
    """
    backup = PDF_BACKUP_DIR / pdf_path.name
    if backup.exists():
        return total, hashes

    keep = pages_to_keep(hashes, total)
    if len(keep) == total:
        return total, hashes  # nothing padded (or hashes unavailable)

    slim_tmp = pdf_path.with_suffix(".slim.pdf")
    try:
        PDF_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, backup)
        slim_pdf(backup, slim_tmp, keep)
        if page_count(slim_tmp) != len(keep):
            raise RuntimeError("slim page count mismatch")
    except (subprocess.CalledProcessError, RuntimeError, OSError):
        slim_tmp.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        return total, hashes  # OCR the original as-is

    slim_tmp.replace(pdf_path)
    issue_dir = out_dir.parent
    (issue_dir / "pagemap.json").write_text(
        json.dumps({"n_before": total, "n_after": len(keep), "kept": keep}), encoding="utf-8")
    remap_ocr_pages(out_dir, keep)
    new_hashes = {new: hashes[old] for new, old in enumerate(keep, 1) if old in hashes}
    return len(keep), new_hashes


def render_page(pdf_path, page, dest_png, dpi):
    subprocess.run(
        ["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi),
         "-png", "-singlefile", str(pdf_path), str(dest_png.with_suffix(""))],
        check=True, capture_output=True,
    )


def ocr_image(png_path):
    return subprocess.run(
        ["tesseract", str(png_path), "-", "-l", OCR_LANG],
        check=True, capture_output=True, text=True,
    ).stdout


def atomic_write(path, text):
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def process_issue(pdf_path, dpi, dedupe=True):
    POWER_OK.wait()  # block here while the laptop is on battery

    out_dir = DOWNLOAD_DIR / pdf_path.stem / "pdf-pages"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        total = page_count(pdf_path)
    except subprocess.CalledProcessError:
        return Result(pdf_path.name, 0, 0, 0, "pdfinfo failed")

    needs_dedupe = dedupe and not (PDF_BACKUP_DIR / pdf_path.name).exists()
    todo = [n for n in range(1, total + 1) if not (out_dir / f"page-{n}.txt").exists()]
    if not todo and not needs_dedupe:
        return Result(pdf_path.name, total, 0, 0, None)

    # Born-digital issues: no scan padding to slim, and per-page image hashes
    # are meaningless (pages share logos/ads), so skip dedupe and the hash cache.
    layer = text_layer_pages(pdf_path)
    if layer:
        needs_dedupe = False
        if not todo:
            return Result(pdf_path.name, total, 0, 0, None)

    dedupe_note = None
    new = errors = from_layer = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        hashes = {} if layer else page_image_hashes(pdf_path, tmp)

        if needs_dedupe:
            before = total
            total, hashes = dedupe_issue(pdf_path, hashes, total, out_dir)
            if total != before:
                dedupe_note = f"deduped {before}->{total}"
                todo = [n for n in range(1, total + 1)
                        if not (out_dir / f"page-{n}.txt").exists()]

        if not todo:
            return Result(pdf_path.name, total, 0, 0, dedupe_note)

        for page in todo:
            POWER_OK.wait()  # stop mid-issue too if we get unplugged
            dest = out_dir / f"page-{page}.txt"
            digest = hashes.get(page)
            cache_file = HASH_CACHE_DIR / f"{digest}.txt" if digest else None
            try:
                if len("".join(layer.get(page, "").split())) >= MIN_TEXT_LAYER_CHARS:
                    text = layer[page]
                    from_layer += 1
                elif cache_file and cache_file.exists():
                    text = cache_file.read_text(encoding="utf-8")
                else:
                    png = tmp / f"page-{page}.png"
                    render_page(pdf_path, page, png, dpi)
                    text = DEHYPHEN_RE.sub("", ocr_image(png))
                    png.unlink(missing_ok=True)
                    if cache_file:
                        atomic_write(cache_file, text)
                atomic_write(dest, text)
                new += 1
            except (subprocess.CalledProcessError, OSError):
                errors += 1  # leave the page unwritten so a later run retries it

    parts = [p for p in (dedupe_note,
                         f"{from_layer} from text layer" if from_layer else None,
                         f"{errors} page errors" if errors else None) if p]
    return Result(pdf_path.name, total, new, errors, ", ".join(parts) or None)


def issue_sort_key(pdf_path):
    year, issue = pdf_path.stem.split("-")[-2:]  # spiegel-1990-3, spiegel-spjc-2025-1
    return int(year), int(issue)


def fmt_eta(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, default=min(14, os.cpu_count() or 4))
    parser.add_argument("--from", dest="from_year", type=int, default=0)
    parser.add_argument("--to", dest="to_year", type=int, default=9999)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--run-on-battery", action="store_true",
                        help="keep OCRing on battery (default: pause until plugged in)")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="don't slim scan-padding dupes out of not-yet-processed issue PDFs first")
    args = parser.parse_args()

    if not args.run_on_battery:
        threading.Thread(target=power_monitor, daemon=True).start()
        if not on_ac_power():
            POWER_OK.clear()
            print(f"[{time.strftime('%H:%M:%S')}] on battery - waiting for AC power before starting", flush=True)

    HASH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for leftover in HASH_CACHE_DIR.glob(".*.tmp"):
        leftover.unlink(missing_ok=True)

    pdfs = [
        p for p in sorted(DOWNLOAD_DIR.glob("spiegel-*.pdf"), key=issue_sort_key)
        if args.from_year <= issue_sort_key(p)[0] <= args.to_year
    ]
    if not pdfs:
        raise SystemExit("no matching issue PDFs")

    power = "run-on-battery" if args.run_on_battery else "pause-on-battery"
    dd = "no-dedupe" if args.no_dedupe else "dedupe-new"
    print(f"{len(pdfs)} issues | {args.workers} workers | {args.dpi} dpi | lang={OCR_LANG} | {power} | {dd}", flush=True)
    start = time.time()
    done = pages_new = issues_with_errors = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_issue, pdf, args.dpi, not args.no_dedupe): pdf for pdf in pdfs}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:  # keep the run alive whatever one issue does
                result = Result(futures[future].name, 0, 0, 1, f"crashed: {exc}")

            done += 1
            pages_new += result.new
            if result.errors:
                issues_with_errors += 1

            elapsed = time.time() - start
            rate = pages_new / elapsed if elapsed else 0
            eta = fmt_eta((len(pdfs) - done) / (done / elapsed)) if done else "?"
            note = f"  [{result.note}]" if result.note else ""
            print(
                f"[{done:>4}/{len(pdfs)}] {result.name:<22} "
                f"{result.total:>3}p {result.new:>3} new | {rate:5.1f} pg/s | eta {eta}{note}",
                flush=True,
            )

    print(
        f"finished: {pages_new} new pages in {fmt_eta(time.time() - start)} "
        f"({issues_with_errors} issues had page errors)",
        flush=True,
    )


if __name__ == "__main__":
    main()
