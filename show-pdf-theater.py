import re
from pathlib import Path

from spiegel_config import DATA_DIR, RESULTS_DIR

DOWNLOAD_DIR = DATA_DIR
OUTPUT_DIR = RESULTS_DIR / "pdf-theater"

# Plain substrings match compound words too (e.g. "Theaterkritiker"), which
# is what we want - except "regie" and "oper" also occur inside common,
# unrelated words ("Regierung", "Operation"), so those two need a negative
# lookahead to exclude those specific continuations.
THEATER_PATTERNS = [re.compile(p) for p in [
    "theater", "bühne", "schauspiel", r"regie(?!r)", "intendant",
    "uraufführung", "ensemble", r"drama(?!tisch)", "komödie", r"oper(?!ation|ieren)", "operette",
]]
CINEMA_PATTERNS = [re.compile(p) for p in [
    "film", "kino", "drehbuch", "leinwand", "kamera", "hollywood",
]]


def count_pattern_hits(text, patterns):
    text_lower = text.lower()
    return sum(len(pattern.findall(text_lower)) for pattern in patterns)


def is_about_theater(text):
    theater_hits = count_pattern_hits(text, THEATER_PATTERNS)
    cinema_hits = count_pattern_hits(text, CINEMA_PATTERNS)
    return theater_hits > 0 and theater_hits >= cinema_hits


def issue_sort_key(pdf_path):
    _, year, issue = pdf_path.stem.split("-")
    return int(year), int(issue)


def main():
    for pdf_path in sorted(DOWNLOAD_DIR.glob("spiegel-*.pdf"), key=issue_sort_key):
        _, year, issue = pdf_path.stem.split("-")
        pages_dir = DOWNLOAD_DIR / f"spiegel-{year}-{issue}" / "pdf-pages"

        for text_file in sorted(pages_dir.glob("page-*.txt"), key=lambda p: int(p.stem.split("-")[1])):
            text = text_file.read_text(encoding="utf-8")
            if not is_about_theater(text):
                continue

            year_dir = OUTPUT_DIR / year
            year_dir.mkdir(parents=True, exist_ok=True)
            page_num = text_file.stem.split("-")[1]
            dest = year_dir / f"spiegel-{year}-{issue}-page{page_num}.txt"
            dest.write_text(text, encoding="utf-8")
            print(f"saved {dest}")


if __name__ == "__main__":
    main()
