#!/usr/bin/env python3
"""Extract each issue's table of contents from the OCR'd page text and pull
out the Theater (and stage-adjacent) entries.

Reads downloads/spiegel-<y>-<n>/pdf-pages/page-*.txt (from ocr-pdfs.py) and
writes, under data/toc/:

    <y>-<n>.json    full per-issue parse
    theater.tsv     one row per Theater / stage entry: year issue page rubric title
    coverage.tsv    per issue: which page the contents was found on, and why not

The contents list moved around over the decades: a small boxed "INHALT" on
one of the last pages in the late 1940s, a full "IN DIESEM HEFT" page near
the front from the 1960s on. The finder scans both ends of each issue and
scores pages for how much they look like a table of contents.

    .venv/bin/python3 extract/extract-toc.py [--from Y] [--to Y] [--issue Y-N]
                                     [--show Y-N] [--rescan]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spiegel_config import DATA_DIR, RESULTS_DIR  # noqa: E402

DOWNLOAD_DIR = DATA_DIR
OUT_DIR = RESULTS_DIR / "toc"

FRONT_PAGES = 8          # how many pages from the front to consider
BACK_PAGES = 8           # ... and from the back
CONTENTS_SPILL = 2       # extra pages after the best one that may continue it

MODERN_HEADER_RE = re.compile(r"IN DIESEM HEFT|IN DIESER AUSGABE", re.I)
INHALT_LINE_RE = re.compile(r"^[\s.:·—\-\"„]*(?:DER\s+)?[1I]NHALT\b.{0,5}$", re.I)
MASTHEAD_RE = re.compile(
    r"DER\s+SPIEGEL|NACHRICHTEN-?MAGAZIN|^\s*Redakti|Chefredakteur|Herausgeber|"
    r"SPIEGEL-VERLAG|Sammelnummer", re.I | re.M)
SEITE_RE = re.compile(r"\bSeite\s+(\d{1,3})\b", re.I)
TRAILING_NUM_RE = re.compile(r"(?:^|[\s.·—-])(\d{1,3})\s*$")
UPPER = "A-ZÄÖÜ"
LOWER = "a-zäöüß"

# Real Spiegel print rubrics (print era + the scraped section vocabulary).
# Used to accept multi-word all-caps lines as section headers.
KNOWN_RUBRICS = {
    "DEUTSCHLAND", "AUSLAND", "PANORAMA", "WIRTSCHAFT", "SPORT", "KULTUR",
    "PERSONALIEN", "PRESSE", "KIRCHE", "MEDIZIN", "THEATER", "FILM", "FERNSEHEN",
    "RUNDFUNK", "MUSIK", "KUNST", "LITERATUR", "BÜCHER", "MODE", "RECHT",
    "PROZESSE", "AFFÄREN", "PARTEIEN", "BONN", "BUNDESWEHR", "BUNDESLÄNDER",
    "GEMEINDEN", "MANAGER", "ZEITGESCHICHTE", "HANDEL", "STEUERN", "BEAMTE",
    "SOWJETZONE", "VERKEHR", "SERIE", "ASIEN", "RÜSTUNG", "ITALIEN", "POLEN",
    "USA", "ENGLAND", "FISCHEREI", "MUSICAL", "JAZZ", "RAUMFAHRT", "WISSENSCHAFT",
    "RELIGION", "GESELLSCHAFT", "AUTOMOBILE", "LUFTFAHRT", "ARCHITEKTUR",
    "MALEREI", "FESTSPIELE", "KABARETT", "SCHULE", "HOCHSCHULE", "HOCHSCHULEN",
    "JUSTIZ", "POLIZEI", "SPIONAGE", "VERBRECHEN", "UMWELT", "COMPUTER",
    "TITELGESCHICHTE", "TITEL", "SPIEGEL-GESPRÄCH", "SPIEGEL-GESPRÄCHE",
    "SPIEGEL-INTERVIEW", "SPIEGEL-SERIE", "SPIEGEL-REPORT", "SPIEGEL-VERLAG",
    "HAUSMITTEILUNG", "BRIEFE", "REGISTER", "IMPRESSUM", "DER SPIEGEL",
    "GESUNDHEIT", "TECHNIK", "ENERGIE", "TERRORISMUS", "GEHEIMDIENSTE",
    "SZENE", "TRENDS", "REPORT", "NACHRUF",
}

# Stage / theatre rubric names; OCR near-misses (edit distance <= 1) count too.
STAGE_RUBRICS = {
    "THEATER": "theater",
    "BÜHNE": "buehne",
    "SCHAUSPIEL": "schauspiel",
    "FESTSPIELE": "festspiele",
    "KABARETT": "kabarett",
    "MUSICAL": "musical",
    "OPER": "oper",
    "OPERETTE": "operette",
    "BALLETT": "ballett",
    "TANZ": "tanz",
}


def levenshtein(a, b):
    if a == b:
        return 0
    if not a or not b:
        return len(a) + len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def rubric_stage_kind(rubric):
    """Return the canonical stage-rubric name if `rubric` is one (OCR-fuzzy), else None."""
    norm = re.sub(rf"[^{UPPER}{LOWER}]", "", rubric).upper()
    if len(norm) < 3:
        return None
    if levenshtein(norm, "THEATER") <= 1:
        return "THEATER"
    for canonical in STAGE_RUBRICS:
        tol = 0 if len(canonical) <= 4 else 1
        if levenshtein(norm, canonical) <= tol:
            return canonical
    return None


def page_count(pdf_path, _cache={}):
    if pdf_path not in _cache:
        try:
            out = subprocess.run(["pdfinfo", str(pdf_path)], check=True,
                                 capture_output=True, text=True).stdout
            match = re.search(r"^Pages:\s+(\d+)", out, re.M)
            _cache[pdf_path] = int(match.group(1)) if match else 0
        except subprocess.CalledProcessError:
            _cache[pdf_path] = 0
    return _cache[pdf_path]


def is_rubric_line(line):
    """A standalone section header: all-caps, no lowercase, short."""
    stripped = line.strip(" .:-·—")
    letters = re.sub(rf"[^{UPPER}{LOWER}]", "", stripped)
    if not (3 <= len(letters) <= 24):
        return False
    if re.search(f"[{LOWER}]", stripped):
        return False
    if not re.search(f"[{UPPER}]", stripped):
        return False
    if re.search(r"\d", stripped):
        return False
    tokens = stripped.split()
    if len(tokens) == 1:
        return True
    return stripped.upper() in KNOWN_RUBRICS


def normalize_rubric(line):
    return re.sub(r"\s+", " ", line.strip(" .:-·—")).upper()


def repair_number(token):
    """Best-effort fix of an OCR-mangled page number ('BB', 'l8', 'MR' -> int or None)."""
    fixed = (token.upper()
             .replace("O", "0").replace("Q", "0")
             .replace("I", "1").replace("L", "1").replace("|", "1")
             .replace("B", "8").replace("S", "5").replace("G", "6").replace("Z", "2"))
    digits = re.sub(r"\D", "", fixed)
    if digits and len(digits) <= 3:
        return int(digits)
    return None


def score_modern_page(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    header = 60 if MODERN_HEADER_RE.search("\n".join(lines[:4])) else 0
    seite = len(SEITE_RE.findall(text))
    trailing = sum(1 for ln in lines if TRAILING_NUM_RE.search(ln))
    rubrics = sum(1 for ln in lines if is_rubric_line(ln))
    short = sum(1 for ln in lines if len(ln) < 45) / len(lines)
    return header + 3.0 * seite + 2.5 * rubrics + 1.0 * trailing + 6.0 * short


def load_pages(issue_dir, wanted):
    pages = {}
    for n in wanted:
        f = issue_dir / f"page-{n}.txt"
        if f.exists():
            pages[n] = f.read_text(encoding="utf-8", errors="replace")
    return pages


def candidate_page_numbers(total):
    front = list(range(1, min(FRONT_PAGES, total) + 1))
    back = list(range(max(1, total - BACK_PAGES + 1), total + 1))
    seen, ordered = set(), []
    for n in front + back:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def parse_modern_entries(text):
    """Rubric / title / page triples from an 'IN DIESEM HEFT'-style contents page."""
    entries = []
    rubric = None
    title_buf = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if MODERN_HEADER_RE.search(line):
            continue
        if MASTHEAD_RE.search(line) or re.match(r"DER SPIEGEL\s+\d", line):
            break
        if is_rubric_line(line):
            rubric = normalize_rubric(line)
            title_buf = []
            continue

        m = SEITE_RE.search(line) or TRAILING_NUM_RE.search(line)
        if m:
            page = int(m.group(1))
            head = line[:m.start()].strip(" .:-·—\t")
            title = " ".join([*title_buf, head]).strip() if head else " ".join(title_buf).strip()
            title = re.sub(r"[.·]{2,}", " ", title).strip(" .·—-")
            if title or rubric:
                entries.append({"rubric": rubric, "title": title, "page": page})
            title_buf = []
        else:
            title_buf.append(line)

    return entries


def slice_inhalt_box(text):
    """The lines of an early boxed 'INHALT' list: from the INHALT line to the masthead."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if INHALT_LINE_RE.match(ln)), None)
    if start is None:
        return None
    box = []
    for ln in lines[start + 1:]:
        if MASTHEAD_RE.search(ln):
            break
        box.append(ln)
        if len(box) > 30:
            break
    return "\n".join(box)


def parse_inhalt_box(box_text):
    """'Rubric .... <garbled page>' lines (caps or title-case) - keep only
    recognisable rubrics; the early box carries no article titles."""
    entries = []
    for raw in box_text.splitlines():
        line = raw.strip(" .:-·—\"„'")
        m = re.match(rf"^([{UPPER}][{UPPER}{LOWER}.\- ]{{2,22}}?)(?:[\s.]+(.*))?$", line)
        if not m:
            continue
        rubric = re.sub(r"[.\-]", " ", m.group(1)).strip().upper()
        rubric = re.sub(r"\s+", " ", rubric)
        if rubric not in KNOWN_RUBRICS and not rubric_stage_kind(rubric):
            continue
        tail = (m.group(2) or "").split()
        page = repair_number(tail[-1]) if tail else None
        entries.append({"rubric": rubric, "title": "", "page": page})
    return entries


def find_contents(issue_dir, total):
    wanted = candidate_page_numbers(total)
    pages = load_pages(issue_dir, wanted)
    missing = [n for n in wanted if n not in pages]
    if missing:
        return {"status": "pending-ocr", "missing_pages": missing}

    # Phase 1: a modern "IN DIESEM HEFT" / "IN DIESER AUSGABE" contents page.
    modern = sorted(((score_modern_page(pages[n]), n) for n in wanted), reverse=True)
    best_score, best_page = modern[0]
    if best_score >= 60:  # header bonus means we actually saw the words
        text = pages[best_page]
        for follow in range(best_page + 1, best_page + 1 + CONTENTS_SPILL):
            if follow in pages and score_modern_page(pages[follow]) > 25:
                text += "\n" + pages[follow]
        entries = parse_modern_entries(text)
        for e in entries:
            e["stage_kind"] = rubric_stage_kind(e["rubric"] or "")
        return {"status": "ok", "contents_page": best_page, "contents_score": round(best_score, 1),
                "header": "IN DIESEM HEFT", "entries": entries}

    # Phase 2: an early boxed "INHALT" list, usually on one of the last pages.
    for n in wanted:
        box = slice_inhalt_box(pages[n])
        if box is None:
            continue
        entries = parse_inhalt_box(box)
        if not entries:
            continue
        for e in entries:
            e["stage_kind"] = rubric_stage_kind(e["rubric"] or "")
        return {"status": "ok", "contents_page": n, "contents_score": None,
                "header": "INHALT", "entries": entries}

    return {"status": "no-contents-found", "best_page": best_page,
            "best_score": round(best_score, 1)}


def issue_key(pdf_path):
    year, issue = pdf_path.stem.split("-")[1:]
    return int(year), int(issue)


def process(pdf_path, rescan):
    year, issue = issue_key(pdf_path)
    issue_dir = DOWNLOAD_DIR / f"spiegel-{year}-{issue}" / "pdf-pages"
    out_json = OUT_DIR / f"{year}-{issue}.json"

    if not issue_dir.is_dir():
        return {"year": year, "issue": issue, "status": "no-ocr"}
    if out_json.exists() and not rescan:
        return json.loads(out_json.read_text(encoding="utf-8"))

    total = page_count(pdf_path)
    if not total:
        return {"year": year, "issue": issue, "status": "no-pdfinfo"}

    result = {"year": year, "issue": issue, "pages": total, **find_contents(issue_dir, total)}
    if result["status"] == "ok":
        out_json.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def write_tables(results):
    theater_rows = []
    coverage_rows = []
    for r in sorted(results, key=lambda r: (r["year"], r["issue"])):
        entries = r.get("entries", [])
        stage = [e for e in entries if e.get("stage_kind")]
        theater = [e for e in stage if e["stage_kind"] == "THEATER"]
        coverage_rows.append("\t".join(str(x) for x in [
            r["year"], r["issue"], r.get("pages", ""), r["status"],
            r.get("contents_page", ""), r.get("header", "") or "",
            len(entries), len(theater), len(stage),
        ]))
        for e in stage:
            theater_rows.append("\t".join(str(x) for x in [
                r["year"], r["issue"], e.get("page") if e.get("page") is not None else "",
                e["stage_kind"], e["rubric"] or "", e["title"],
            ]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "coverage.tsv").write_text(
        "year\tissue\tpages\tstatus\tcontents_page\theader\tn_entries\tn_theater\tn_stage\n"
        + "\n".join(coverage_rows) + "\n", encoding="utf-8")
    (OUT_DIR / "theater.tsv").write_text(
        "year\tissue\tpage\tkind\trubric\ttitle\n" + "\n".join(theater_rows) + "\n",
        encoding="utf-8")
    return len(theater_rows), coverage_rows


def show_one(pdf_path):
    year, issue = issue_key(pdf_path)
    issue_dir = DOWNLOAD_DIR / f"spiegel-{year}-{issue}" / "pdf-pages"
    total = page_count(pdf_path)
    print(f"# {year}-{issue}  ({total} pages)")
    wanted = candidate_page_numbers(total)
    pages = load_pages(issue_dir, wanted)
    for n in wanted:
        if n in pages:
            print(f"  page {n:>3}: modern-score {score_modern_page(pages[n]):6.1f}"
                  f"{'  [has INHALT box]' if slice_inhalt_box(pages[n]) is not None else ''}")
    res = find_contents(issue_dir, total)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("status") == "ok":
        print(f"\n--- contents page {res['contents_page']} ---")
        print(pages[res["contents_page"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_year", type=int, default=0)
    parser.add_argument("--to", dest="to_year", type=int, default=9999)
    parser.add_argument("--issue", help="only this issue, e.g. 1965-1")
    parser.add_argument("--show", help="print the contents-page detection for one issue, e.g. 1965-1")
    parser.add_argument("--rescan", action="store_true", help="ignore cached per-issue JSON")
    args = parser.parse_args()

    # regular issues only; special editions (spiegel-spjc-2025-1, ...) have no year-issue stem
    pdfs = sorted(DOWNLOAD_DIR.glob("spiegel-[0-9]*.pdf"), key=issue_key)

    if args.show:
        y, i = map(int, args.show.split("-"))
        show_one(DOWNLOAD_DIR / f"spiegel-{y}-{i}.pdf")
        return

    if args.issue:
        y, i = map(int, args.issue.split("-"))
        pdfs = [p for p in pdfs if issue_key(p) == (y, i)]
    else:
        pdfs = [p for p in pdfs if args.from_year <= issue_key(p)[0] <= args.to_year]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    status_counts = {}
    for pdf in pdfs:
        r = process(pdf, args.rescan)
        results.append(r)
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    n_theater, _ = write_tables(results)
    print(f"{len(results)} issues:", ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())))
    print(f"-> data/toc/theater.tsv ({n_theater} stage entries), data/toc/coverage.tsv")


if __name__ == "__main__":
    main()
