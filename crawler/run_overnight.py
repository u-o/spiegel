import datetime
import os
import signal
import subprocess

import crawler

DEADLINE_HOUR = 7  # stop the crawl by this hour, whatever year/issue it's reached


def _ocr_process_groups():
    """Process groups of any running ocr-pdfs.py (the bulk PDF OCR job)."""
    out = subprocess.run(["pgrep", "-f", "ocr-pdfs.py"], capture_output=True, text=True).stdout
    groups = set()
    for pid in out.split():
        try:
            groups.add(os.getpgid(int(pid)))
        except (ProcessLookupError, ValueError):
            pass
    return groups


def _signal_groups(groups, sig):
    for pgid in groups:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass


def main():
    now = datetime.datetime.now()
    deadline = now.replace(hour=DEADLINE_HOUR, minute=0, second=0, microsecond=0)
    if deadline <= now:
        deadline += datetime.timedelta(days=1)

    # The bulk OCR run saturates every core; left running it starves the
    # crawler's HTTP requests (dropped connections, endless retries). The
    # crawl is the time-boxed job, so freeze OCR for its window and thaw it
    # afterwards - SIGSTOP loses no work.
    ocr_groups = _ocr_process_groups()
    if ocr_groups:
        _signal_groups(ocr_groups, signal.SIGSTOP)
        print(f"Paused background OCR ({len(ocr_groups)} process group(s)) for the crawl window.")

    print(f"Crawling from 1947 onward (skipping anything already downloaded) until {deadline:%Y-%m-%d %H:%M}.")
    try:
        crawler.main(1947, 2027, deadline=deadline)
    finally:
        if ocr_groups:
            _signal_groups(ocr_groups, signal.SIGCONT)
            print("Resumed background OCR.")


if __name__ == "__main__":
    main()
