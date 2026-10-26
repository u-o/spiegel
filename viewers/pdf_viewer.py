#!/usr/bin/env python3
"""Side-by-side PDF browser for the downloaded Spiegel print issues.

Opens a local web page that shows each issue's cover page and its
contents page next to each other, and lets you walk the whole archive
from the keyboard:

    <-  / ->     previous / next issue
    Up / Down    move the right-hand (contents) page within the current issue
    PgUp / PgDn   jump 10 issues
    Home / End   first / last issue

The contents-page number is remembered between issues (the "Inhalt" sits
on a fairly stable page for a given era), so once you nudge it onto the
right page it stays there as you browse.

Standard library only - run it with the project venv:

    .venv/bin/python3 viewers/pdf_viewer.py [port]
"""

from __future__ import annotations

import html
import json
import re
import socket
import sys
import threading
import webbrowser
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from spiegel_config import DATA_DIR  # noqa: E402

DOWNLOAD_DIR = DATA_DIR

ISSUE_STEM_RE = re.compile(r"^spiegel-(\d{4})-(\d+)$")
KEY_RE = re.compile(r"^(\d{4})-(\d+)$")
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.I | re.S)
SOCIAL_TITLE_RE = re.compile(r'"socialMediaTitle"\s*:\s*"([^"]{1,200})"')
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def discover_issues():
    """Every downloaded issue PDF, in chronological (year, issue) order."""
    issues = []
    for pdf in DOWNLOAD_DIR.glob("spiegel-*.pdf"):
        match = ISSUE_STEM_RE.match(pdf.stem)
        if match:
            issues.append({"year": int(match.group(1)), "issue": int(match.group(2))})
    issues.sort(key=lambda d: (d["year"], d["issue"]))
    return issues


@lru_cache(maxsize=None)
def issue_headline(year, issue):
    """The cover-story headline for an issue, pulled from its scraped index page.

    The first <h2> on the issue page is the Titelgeschichte teaser
    ("Entscheidung am Golf", "Zurück zur Entspannung?", ...); a
    "socialMediaTitle" field in the page's inline JSON is the fallback.
    Returns an empty string when neither is usable or no HTML is on disk.
    """
    html_path = DOWNLOAD_DIR / f"spiegel-{year}-{issue}.html"
    if not html_path.exists():
        return ""
    text = html_path.read_text(encoding="utf-8", errors="replace")

    match = H2_RE.search(text)
    if match:
        headline = WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", match.group(1)))).strip()
        if headline:
            return headline

    match = SOCIAL_TITLE_RE.search(text)
    if match:
        raw = html.unescape(match.group(1)).replace("\\", "").strip()
        return re.sub(r"^\(S\+\)\s*", "", raw).strip()

    return ""


def pdf_path_for(key):
    match = KEY_RE.match(key)
    if not match:
        return None
    path = DOWNLOAD_DIR / f"spiegel-{int(match.group(1))}-{int(match.group(2))}.pdf"
    return path if path.exists() else None


PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spiegel PDF viewer</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    display: flex; flex-direction: column;
    font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #14161a; color: #e8e6e3;
  }
  header {
    flex: 0 0 auto; display: flex; align-items: center; gap: 16px;
    padding: 10px 16px; background: #1d2026; border-bottom: 1px solid #2c313a;
  }
  header .nav { display: flex; align-items: center; gap: 6px; }
  header button {
    font: inherit; color: inherit; background: #2c313a; border: 1px solid #3a414d;
    border-radius: 6px; padding: 4px 10px; cursor: pointer;
  }
  header button:hover { background: #363d49; }
  header .label { font-weight: 600; font-size: 15px; white-space: nowrap; }
  header .headline {
    flex: 1 1 auto; min-width: 0; color: #b7c4d8;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  header .meta { color: #8a93a3; white-space: nowrap; }
  header input {
    font: inherit; color: inherit; background: #14161a;
    border: 1px solid #3a414d; border-radius: 6px; padding: 4px 8px; width: 90px;
  }
  .panes { flex: 1 1 auto; display: flex; min-height: 0; }
  .pane { flex: 1 1 50%; min-width: 0; position: relative; }
  .pane + .pane { border-left: 1px solid #2c313a; }
  .pane .cap {
    position: absolute; top: 8px; left: 8px; z-index: 2;
    background: rgba(20,22,26,.82); color: #b7c4d8;
    padding: 2px 8px; border-radius: 5px; font-size: 12px; pointer-events: none;
  }
  iframe { width: 100%; height: 100%; border: 0; background: #333; }
  .empty { margin: auto; color: #8a93a3; padding: 40px; text-align: center; }
</style>
</head>
<body>
<header>
  <div class="nav">
    <button id="prev" title="Previous issue (Left arrow)">&larr;</button>
    <button id="next" title="Next issue (Right arrow)">&rarr;</button>
  </div>
  <span class="label" id="label">-</span>
  <span class="headline" id="headline"></span>
  <span class="meta" id="counter"></span>
  <span class="meta">contents p.<span id="cpage">3</span></span>
  <input id="jump" placeholder="1991-3" title="Jump to issue (year-number)">
</header>
<div class="panes" id="panes">
  <div class="pane"><span class="cap">cover</span><iframe id="left"></iframe></div>
  <div class="pane"><span class="cap" id="rcap">contents</span><iframe id="right"></iframe></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let issues = [];
let idx = 0;
let contentsPage = 3;
let blobUrl = null;
let loadToken = 0;

function clampInt(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function restore() {
  const s = parseInt(localStorage.getItem("spv_contentsPage"), 10);
  if (!Number.isNaN(s)) contentsPage = clampInt(s, 1, 999);
}
function save() {
  localStorage.setItem("spv_idx", String(idx));
  localStorage.setItem("spv_contentsPage", String(contentsPage));
}

function paintRight() {
  $("cpage").textContent = contentsPage;
  $("rcap").textContent = "contents · p." + contentsPage;
  if (blobUrl) $("right").src = blobUrl + "#page=" + contentsPage + "&view=FitH";
}

async function loadIssue() {
  const token = ++loadToken;
  const cur = issues[idx];
  const key = cur.year + "-" + cur.issue;
  $("label").textContent = cur.issue + "/" + cur.year;
  $("counter").textContent = "issue " + (idx + 1) + " / " + issues.length;
  $("headline").textContent = "";
  save();

  fetch("/api/headline/" + key).then(r => r.json()).then(d => {
    if (token === loadToken) $("headline").textContent = d.headline || "";
  }).catch(() => {});

  if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
  $("left").removeAttribute("src");
  $("right").removeAttribute("src");
  try {
    const resp = await fetch("/pdf/" + key);
    if (!resp.ok) throw new Error("http " + resp.status);
    const blob = await resp.blob();
    if (token !== loadToken) return;            // a newer navigation won
    blobUrl = URL.createObjectURL(blob);
    $("left").src = blobUrl + "#page=1&view=FitH";
    paintRight();
  } catch (e) {
    if (token === loadToken) $("headline").textContent = "(failed to load PDF: " + e.message + ")";
  }
}

function go(delta) {
  const next = clampInt(idx + delta, 0, issues.length - 1);
  if (next !== idx) { idx = next; loadIssue(); }
}
function setContentsPage(delta) {
  const next = clampInt(contentsPage + delta, 1, 999);
  if (next !== contentsPage) { contentsPage = next; save(); paintRight(); }
}

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  switch (e.key) {
    case "ArrowLeft":  go(-1); break;
    case "ArrowRight": go(1); break;
    case "ArrowUp":    setContentsPage(1); break;
    case "ArrowDown":  setContentsPage(-1); break;
    case "PageUp":     go(-10); break;
    case "PageDown":   go(10); break;
    case "Home":       idx = 0; loadIssue(); break;
    case "End":        idx = issues.length - 1; loadIssue(); break;
    default: return;
  }
  e.preventDefault();
});

$("prev").addEventListener("click", () => go(-1));
$("next").addEventListener("click", () => go(1));
$("jump").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const m = e.target.value.trim().match(/^(\\d{4})[-/ ](\\d+)$/);
  if (!m) return;
  const year = +m[1], issue = +m[2];
  const found = issues.findIndex(d => d.year === year && d.issue === issue);
  if (found >= 0) { idx = found; e.target.value = ""; e.target.blur(); loadIssue(); }
  else { e.target.value = "not found"; }
});

(async function init() {
  restore();
  issues = await fetch("/api/issues").then(r => r.json());
  if (!issues.length) {
    $("panes").innerHTML = '<div class="empty">No issue PDFs found in downloads/.</div>';
    return;
  }
  const savedIdx = parseInt(localStorage.getItem("spv_idx"), 10);
  idx = Number.isNaN(savedIdx) ? 0 : clampInt(savedIdx, 0, issues.length - 1);
  loadIssue();
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, body, content_type, status=200, extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path == "/":
            self._send(PAGE_HTML, "text/html; charset=utf-8")
            return

        if path == "/api/issues":
            self._send(json.dumps(ISSUES), "application/json")
            return

        if path.startswith("/api/headline/"):
            key = path[len("/api/headline/"):]
            match = KEY_RE.match(key)
            headline = issue_headline(int(match.group(1)), int(match.group(2))) if match else ""
            self._send(json.dumps({"headline": headline}), "application/json")
            return

        if path.startswith("/pdf/"):
            self._serve_pdf(path[len("/pdf/"):])
            return

        self._send("not found", "text/plain", status=404)

    do_HEAD = do_GET

    def _serve_pdf(self, key):
        pdf_path = pdf_path_for(key)
        if pdf_path is None:
            self._send("no such issue", "text/plain", status=404)
            return

        data = pdf_path.read_bytes()
        total = len(data)
        range_header = self.headers.get("Range")
        start, end = 0, total - 1

        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            lo, _, hi = spec.partition("-")
            try:
                if lo:
                    start = int(lo)
                    end = int(hi) if hi else total - 1
                else:  # suffix range: bytes=-N
                    start = max(0, total - int(hi))
                    end = total - 1
            except ValueError:
                start, end = 0, total - 1
            start = max(0, start)
            end = min(end, total - 1)

        chunk = data[start:end + 1]
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        }
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            self._send(chunk, "application/pdf", status=206, extra_headers=headers)
        else:
            self._send(chunk, "application/pdf", extra_headers=headers)


def pick_port(preferred):
    for candidate in [preferred, 0]:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", candidate))
            port = probe.getsockname()[1]
            probe.close()
            return port
        except OSError:
            continue
    raise SystemExit("could not bind a local port")


ISSUES = discover_issues()


def main():
    if not ISSUES:
        raise SystemExit(f"No issue PDFs found in {DOWNLOAD_DIR}")

    preferred = int(sys.argv[1]) if len(sys.argv) > 1 else 8731
    port = pick_port(preferred)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Spiegel PDF viewer: {len(ISSUES)} issues  ->  {url}", flush=True)
    print("Arrow keys: <-/-> issue, Up/Down contents page, Home/End ends. Ctrl+C to stop.", flush=True)
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        server.shutdown()


if __name__ == "__main__":
    main()
