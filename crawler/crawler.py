import datetime
import random
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spiegel_config import DATA_DIR  # noqa: E402

BASE_URL = "https://www.spiegel.de/spiegel/print"
DOWNLOAD_DIR = DATA_DIR
COOKIES_FILE = Path(__file__).resolve().parent / "cookies.txt"
MIN_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 5.0
TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 10
MAX_PDF_REAUTH_ATTEMPTS = 3

# The User-Agent identifies us to Spiegel (contact address + permission
# ticket), so it lives in an untracked file; see user_agent.example.txt.
USER_AGENT_FILE = Path(__file__).resolve().parent / "user_agent.txt"
if not USER_AGENT_FILE.exists():
    raise SystemExit(f"Missing {USER_AGENT_FILE} - copy user_agent.example.txt and fill it in.")
HEADERS = {"User-Agent": USER_AGENT_FILE.read_text(encoding="utf-8").strip()}

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

session = requests.Session()
session.headers.update(HEADERS)


def _load_cookies(path):
    """Parse a Netscape-format cookies.txt (tab-separated) into a cookie jar.

    Not using http.cookiejar.MozillaCookieJar here: it enforces a
    domain/leading-dot consistency rule that many real browser cookie
    exports violate, so it rejects files that work fine in practice.
    """
    jar = requests.cookies.RequestsCookieJar()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        domain, _, cookie_path, secure, _, name, value = line.split("\t")
        jar.set(name, value, domain=domain, path=cookie_path, secure=secure == "TRUE")
    return jar


def _save_cookies(path):
    """Write the live session's cookie jar back out in Netscape cookies.txt format.

    Skips the write entirely if boCmtSession has dropped out of the jar
    (rather than just going stale) - that happens when a renewal attempt
    fails outright (e.g. a forced logout) instead of rotating in a fresh
    one, and persisting that loss would strand every future run with no
    session cookie at all. Leaving the last-known-good file in place means
    a following run at least starts from a cookie that used to work.

    gruppenkonto.spiegel.de silently rotates boCmtSession (and the
    single-use boCmtRememberme token used to mint a fresh one) whenever a
    stale session gets renewed via the remember-me flow. Persisting after
    every request means that rotation survives process restarts instead of
    being lost the moment this script exits.
    """
    if "boCmtSession" not in session.cookies.get_dict():
        return

    lines = [
        "\t".join([
            cookie.domain,
            "TRUE",
            cookie.path,
            "TRUE" if cookie.secure else "FALSE",
            str(int(cookie.expires)) if cookie.expires else "0",
            cookie.name,
            cookie.value,
        ])
        for cookie in session.cookies
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if COOKIES_FILE.exists():
    session.cookies = _load_cookies(COOKIES_FILE)


def fetch(url):
    """GET a URL politely: a random delay, then the raw response.

    Retries on transient network errors (timeouts, dropped connections) with
    a backoff, since an unattended overnight run shouldn't die on one bad
    connection.
    """
    time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            # A connection cut mid-body can still hand back a short response;
            # treat it like any other transient failure and retry.
            expected = response.headers.get("Content-Length")
            if expected and not response.headers.get("Content-Encoding") and len(response.content) != int(expected):
                raise requests.exceptions.ChunkedEncodingError(
                    f"truncated body: got {len(response.content)} of {expected} bytes"
                )
            _save_cookies(COOKIES_FILE)
            return response
        except requests.exceptions.RequestException as error:
            if attempt == MAX_RETRIES:
                raise
            print(f"{url} attempt {attempt}/{MAX_RETRIES} failed ({error}), retrying...")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)


def issue_number(issue_url):
    return int(issue_url.removesuffix(".html").rsplit("-", 1)[1])


def issue_stem(year, issue_url):
    """File stem for an issue; special editions (e.g. /spjc/ = SPIEGEL Chronik)
    get their own prefix so they don't collide with the regular issue number."""
    kind = issue_url.rstrip("/").split("/")[-2]
    prefix = "spiegel" if kind == "print" else f"spiegel-{kind}"
    return f"{prefix}-{year}-{issue_number(issue_url)}"


def get_issue_urls(year):
    """Return all issue URLs listed on a year's overview page, in issue order."""
    year_page = DOWNLOAD_DIR / f"_year-{year}.html"
    if year_page.exists():
        html = year_page.read_text(encoding="utf-8")
    else:
        html = fetch(f"{BASE_URL}/index-{year}.html").text
        year_page.write_text(html, encoding="utf-8")

    soup = BeautifulSoup(html, "html.parser")
    issue_urls = {
        link["href"]
        for link in soup.find_all("a", href=True)
        if f"/index-{year}-" in link["href"]
    }
    return sorted(issue_urls, key=issue_number)


def download_issue_page(year, issue_url):
    """Download an issue's HTML page, unless already cached."""
    dest = DOWNLOAD_DIR / f"{issue_stem(year, issue_url)}.html"
    if not dest.exists():
        dest.write_text(fetch(issue_url).text, encoding="utf-8")
    return dest


class SessionExpiredError(RuntimeError):
    pass


def download_issue_pdf(year, issue_url, issue_html_path):
    """Download an issue's PDF, unless already cached.

    Requires a logged-in session: export cookies from a browser signed in to
    a Spiegel account with PDF access into COOKIES_FILE (Netscape cookies.txt
    format).
    """
    dest = DOWNLOAD_DIR / f"{issue_stem(year, issue_url)}.pdf"
    if dest.exists():
        return dest

    soup = BeautifulSoup(issue_html_path.read_text(encoding="utf-8"), "html.parser")
    pdf_link = soup.find("a", title="PDF")
    if not pdf_link:
        return None
    pdf_url = pdf_link["href"].replace("/download/start.html", "/download/download.html")

    # A stale boCmtSession gets silently renewed via the remember-me cookie:
    # the request redirects through /authenticate, lands on the HTML
    # "start.html" landing page rather than the PDF, but leaves a fresh
    # boCmtSession behind. Usually one retry with that renewed session gets
    # the PDF, but a single renewal doesn't always land a fully working
    # session on the very next attempt, so keep retrying as long as each
    # failure still comes with a fresh re-auth; if a request fails without
    # even triggering that redirect, remember-me itself is spent and further
    # attempts won't help.
    for attempt in range(1, MAX_PDF_REAUTH_ATTEMPTS + 1):
        response = fetch(pdf_url)
        content_type = response.headers.get("Content-Type", "")
        if "application/pdf" in content_type:
            # Never cache a cut-off PDF: an existing file is skipped forever.
            if b"%%EOF" not in response.content[-2048:]:
                raise requests.exceptions.RequestException(f"incomplete PDF for {issue_url}")
            dest.write_bytes(response.content)
            return dest
        reauthenticated = any("/authenticate" in r.url for r in response.history)
        if attempt < MAX_PDF_REAUTH_ATTEMPTS and reauthenticated:
            continue
        raise SessionExpiredError(
            f"Expected a PDF for {issue_url} but got '{content_type}'. "
            f"Your {COOKIES_FILE} session may be missing or expired - "
            "re-export cookies from a browser logged in to a Spiegel account with PDF access."
        )


def get_articles(issue_html_path):
    """Return every article (title, section, link) across all sections of one issue."""
    soup = BeautifulSoup(issue_html_path.read_text(encoding="utf-8"), "html.parser")
    articles = []

    for section_tag in soup.find_all("section", attrs={"data-area": lambda v: v and v.startswith("block>topic:")}):
        title_tag = section_tag.find(attrs={"data-title": True})
        if not title_tag:
            continue
        section_title = title_tag.get_text(strip=True)

        for article_tag in section_tag.find_all("article", attrs={"data-sara-article-id": True}):
            link_tag = article_tag.find("a", href=True)
            if link_tag:
                articles.append({
                    "title": article_tag.get("aria-label", "").strip(),
                    "section": section_title,
                    "link": link_tag["href"],
                })

    return articles


def get_article_text(article_url):
    """Fetch an article page and return its plain body text."""
    soup = BeautifulSoup(fetch(article_url).text, "html.parser")
    paragraphs = []
    for text_block in soup.find_all("div", attrs={"data-area": "text"}):
        paragraphs.extend(p.get_text(" ", strip=True) for p in text_block.find_all("p"))
    return "\n\n".join(paragraphs)


def safe_filename(text):
    return re.sub(r"[^\w\-]+", "_", text).strip("_")[:80]


def article_id(article_url):
    """The unique id Spiegel embeds in every article slug (after '-a-')."""
    return article_url.split("?", 1)[0].rsplit("-a-", 1)[-1]


def download_issue_articles(year, issue_url, issue_html_path):
    """Download the plaintext of every article in one issue, grouped by section."""
    issue_dir = DOWNLOAD_DIR / issue_stem(year, issue_url)

    for article in get_articles(issue_html_path):
        section_dir = issue_dir / safe_filename(article["section"])
        section_dir.mkdir(parents=True, exist_ok=True)
        dest = section_dir / f"{safe_filename(article_id(article['link']))}-{safe_filename(article['title'])}.txt"
        if dest.exists():
            continue
        text = get_article_text(article["link"])
        dest.write_text(f"{article['title']}\n\n{text}", encoding="utf-8")

    return issue_dir


def main(start_year=1947, end_year=1990, deadline=None):
    """Download HTML, PDF, and article plaintext for every issue in range.

    Articles don't need the login session, only PDFs do. So once the PDF
    session dies, we stop attempting PDFs (no point wasting a request per
    issue on a login that won't succeed) but keep downloading articles,
    since those take far longer overall and shouldn't be cut short by an
    expired cookie.

    Already-downloaded issues are skipped almost instantly (cache checks,
    no network), so this is always safe to (re-)run from the very start:
    with a `deadline`, it just picks up wherever last night left off and
    stops cleanly at the cutoff, rather than needing a manually-chosen
    year range each time.

    A single issue that keeps failing (e.g. a connection timeout that
    outlasts fetch()'s own retries) is skipped rather than left to kill an
    unattended overnight run - whatever it didn't finish (page, PDF,
    articles) is just picked up again next run via the same cache checks.
    """
    pdf_session_dead = False

    for year in range(start_year, end_year):
        for issue_url in get_issue_urls(year):
            if deadline and datetime.datetime.now() >= deadline:
                print(f"Reached {deadline:%H:%M} deadline, stopping for tonight.")
                return

            try:
                html_path = download_issue_page(year, issue_url)

                pdf_path = None
                if not pdf_session_dead:
                    try:
                        pdf_path = download_issue_pdf(year, issue_url, html_path)
                    except SessionExpiredError as error:
                        print(f"PDF session expired ({error}); continuing with articles only.")
                        pdf_session_dead = True

                articles_dir = download_issue_articles(year, issue_url, html_path)
                print(f"{html_path} | {pdf_path} | {articles_dir}")
            except requests.exceptions.RequestException as error:
                print(f"{issue_url} failed after retries ({error}); skipping for now, will retry next run.")


if __name__ == "__main__":
    main()
