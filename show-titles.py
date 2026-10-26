import sys
from pathlib import Path

from bs4 import BeautifulSoup

from spiegel_config import DATA_DIR

DOWNLOAD_DIR = DATA_DIR
DEFAULT_SECTION = "Diverse 1947-1979"


def get_articles(html_path, section_title):
    """Return the [title, category] articles in one issue's given section."""
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    for section_tag in soup.find_all("section", attrs={"data-area": lambda v: v and v.startswith("block>topic:")}):
        title_tag = section_tag.find(attrs={"data-title": True})
        if not title_tag or title_tag.get_text(strip=True) != section_title:
            continue

        articles = []
        for article_tag in section_tag.find_all("article", attrs={"data-sara-article-id": True}):
            category_tag = article_tag.find(attrs={"data-target-teaser-el": "topmark"})
            articles.append({
                "title": article_tag.get("aria-label", "").strip(),
                "category": category_tag.get_text(strip=True) if category_tag else "",
            })
        return articles

    return []


def issue_sort_key(html_path):
    _, year, issue = html_path.stem.split("-")
    return int(year), int(issue)


def main():
    section_title = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SECTION
    html_paths = sorted(DOWNLOAD_DIR.glob("spiegel-*.html"), key=issue_sort_key)

    for html_path in html_paths:
        for article in get_articles(html_path, section_title):
            print(f"{html_path.stem}: [{article['category']}] {article['title']}")


if __name__ == "__main__":
    main()
