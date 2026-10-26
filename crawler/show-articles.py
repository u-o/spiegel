import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

import crawler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spiegel_config import DATA_DIR, RESULTS_DIR  # noqa: E402

DOWNLOAD_DIR = DATA_DIR
ARTICLE_CACHE_DIR = DOWNLOAD_DIR / "articles"
OUTPUT_DIR = RESULTS_DIR / "articles-theater"
DEFAULT_SECTION = "Kultur"

THEATER_KEYWORDS = [
    "theater", "bühne", "schauspiel", "regie", "intendant",
    "uraufführung", "ensemble", "drama", "komödie", "oper", "operette",
]
CINEMA_KEYWORDS = [
    "film", "kino", "drehbuch", "leinwand", "kamera", "hollywood",
]

ARTICLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_articles(html_path, section_title):
    """Return the [title, link] articles in one issue's given section."""
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    for section_tag in soup.find_all("section", attrs={"data-area": lambda v: v and v.startswith("block>topic:")}):
        title_tag = section_tag.find(attrs={"data-title": True})
        if not title_tag or title_tag.get_text(strip=True) != section_title:
            continue

        articles = []
        for article_tag in section_tag.find_all("article", attrs={"data-sara-article-id": True}):
            link_tag = article_tag.find("a", href=True)
            if link_tag:
                articles.append({
                    "title": article_tag.get("aria-label", "").strip(),
                    "link": link_tag["href"],
                })
        return articles

    return []


def get_article_text(article_url):
    """Fetch an article page (cached on disk) and return its plain body text."""
    cache_file = ARTICLE_CACHE_DIR / (article_url.split("?", 1)[0].rsplit("/", 1)[-1] + ".html")
    if cache_file.exists():
        html = cache_file.read_text(encoding="utf-8")
    else:
        html = crawler.fetch(article_url).text
        cache_file.write_text(html, encoding="utf-8")

    soup = BeautifulSoup(html, "html.parser")
    paragraphs = []
    for text_block in soup.find_all("div", attrs={"data-area": "text"}):
        paragraphs.extend(p.get_text(" ", strip=True) for p in text_block.find_all("p"))
    return "\n\n".join(paragraphs)


def count_keyword_hits(text, keywords):
    text_lower = text.lower()
    return sum(text_lower.count(keyword) for keyword in keywords)


def is_about_theater(text):
    theater_hits = count_keyword_hits(text, THEATER_KEYWORDS)
    cinema_hits = count_keyword_hits(text, CINEMA_KEYWORDS)
    return theater_hits > 0 and theater_hits >= cinema_hits


def issue_sort_key(html_path):
    _, year, issue = html_path.stem.split("-")
    return int(year), int(issue)


def safe_filename(title):
    return re.sub(r"[^\w\-]+", "_", title).strip("_")[:80]


def main(section_title=DEFAULT_SECTION):
    html_paths = sorted(DOWNLOAD_DIR.glob("spiegel-*.html"), key=issue_sort_key)

    for html_path in html_paths:
        _, year, issue = html_path.stem.split("-")
        for article in get_articles(html_path, section_title):
            text = get_article_text(article["link"])
            if not is_about_theater(text):
                continue

            year_dir = OUTPUT_DIR / year
            year_dir.mkdir(parents=True, exist_ok=True)
            dest = year_dir / f"{issue}-{safe_filename(article['title'])}.txt"
            dest.write_text(f"{article['title']}\n\n{text}", encoding="utf-8")
            print(f"saved {dest}")


if __name__ == "__main__":
    section = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SECTION
    main(section)
