import subprocess

import crawler

CHECK_URL = "https://gruppenkonto.spiegel.de/meinkonto/uebersicht.html"


def is_session_alive():
    response = crawler.session.get(CHECK_URL, timeout=crawler.TIMEOUT_SECONDS)
    return "anmelden" not in response.url


def notify(title, message):
    subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}"'])


if __name__ == "__main__":
    if is_session_alive():
        print("Session alive, nothing to do.")
    else:
        print("Session expired, sending notification.")
        notify("Spiegel crawler", "Cookie session expired - refresh cookies.txt before midnight for tonight's PDFs.")
