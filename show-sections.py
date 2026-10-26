from pathlib import Path

from bs4 import BeautifulSoup

from spiegel_config import DATA_DIR

DOWNLOAD_DIR = DATA_DIR

EXCLUDED_SECTIONS = {
    "Deutschland",
    "Panorama",
    "In dieser Ausgabe",
    "Ausland",
    "Sport",
    "Wirtschaft",
    "Serie",
    "Presse",
    "Kommentar",
    "Lieber Spiegel-Leser",
    "Lieber SPIEGEL-Leser",
    "Meinung",
    "Wissenschaft+Technik",
    "Kriegsgefangene",
    "Kirche",
    "Zitate",
    "Briefmarken",
    "Medizin",
    "Internationales",
    "Gewerkschaften",
    "Forum",
    "Gesellschaft",
    "Chronik",
    "Sowjetzone",
    "Personalien",
}


def get_section_titles(html_path):
    """Return the ordered list of magazine section titles in one issue."""
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    titles = []

    for section_tag in soup.find_all("section", attrs={"data-area": lambda v: v and v.startswith("block>topic:")}):
        title_tag = section_tag.find(attrs={"data-title": True})
        if title_tag:
            titles.append(title_tag.get_text(strip=True))

    return titles


def issue_sort_key(html_path):
    _, year, issue = html_path.stem.split("-")
    return int(year), int(issue)


def main():
    html_paths = sorted(DOWNLOAD_DIR.glob("spiegel-*.html"), key=issue_sort_key)
    for html_path in html_paths:
        titles = [t for t in get_section_titles(html_path) if t not in EXCLUDED_SECTIONS]
        print(f"{html_path.stem}: {', '.join(titles)}")


if __name__ == "__main__":
    main()
