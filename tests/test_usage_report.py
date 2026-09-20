"""scripts/usage_report.py: the access log turned into visitors and a watch list.

The operator's two questions are "how many real people" and "who is abusing
this", and both are answered from lines the report has to classify correctly:
a bot fetching "/" must not count as a visitor, a person who opened the editor
must, and a scanner's 404s must land on the watch list rather than inflate the
refusal count. The synthetic day below has one of each.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import usage_report

PERSON, BOT, SCANNER = "198.51.100.7", "203.0.113.50", "192.0.2.9"
UPLOADER, SCRIPT = "198.51.100.200", "203.0.113.77"
FF = "Mozilla/5.0 (X11; Linux) Firefox/130"


def _line(ip, method, path, status, us=1200, clen="-", ua=FF, ts="2026-09-19 10:00:00 +0000"):
    return (f"[{ts}] [77325] [INFO] gunicorn.access: {ip} \"{method} {path} HTTP/1.1\" "
            f"{status} 512 {us}us {clen} \"{ua}\"\n")


LOG = [
    # a person: opens the page, the editor fires /outline, downloads a badge
    _line(PERSON, "GET", "/", 200),
    _line(PERSON, "GET", "/static/vendor/model-viewer.min.js", 200),
    _line(PERSON, "POST", "/outline", 200),
    _line(PERSON, "POST", "/generate", 200, us=350000, clen="20480"),
    # a bot: fetches the page and nothing else, with a bot UA
    _line(BOT, "GET", "/", 200, ua="Mozilla/5.0 (compatible; Googlebot/2.1)"),
    # a scanner: probes for admin paths and gets HTML 404s
    _line(SCANNER, "GET", "/wp-login.php", 404, ua="python-requests/2.32"),
    _line(SCANNER, "GET", "/.env", 404, ua="python-requests/2.32"),
    # an uploader: keeps sending huge images that are refused or crawl
    _line(UPLOADER, "POST", "/generate", 413, clen="26000000"),
    _line(UPLOADER, "POST", "/generate", 200, us=45000000, clen="24000000"),
    # a script: downloads badges from curl. Not a person, however many it pulls.
    _line(SCRIPT, "POST", "/generate", 200, clen="900", ua="curl/8.7.1"),
    _line(SCRIPT, "POST", "/outline", 200, clen="900", ua="curl/8.7.1"),
    # a line in the pre-content-length format still parses
    f"[2026-09-19 11:00:00 +0000] [1] [INFO] gunicorn.access: {PERSON} \"GET / HTTP/1.1\" 200 512 900us \"{FF}\"\n",
    # a compose-prefixed line parses too
    f"minibadge-designer-1  | [2026-09-19 12:00:00 +0000] [1] [INFO] gunicorn.access: {PERSON} \"POST /outline HTTP/1.1\" 200 12 800us 300 \"{FF}\"\n",
]


def test_one_person_one_bot_one_scanner_one_uploader_are_told_apart():
    rows = list(usage_report.parse(LOG))
    assert len(rows) == len(LOG), "every line shape must parse"
    out = io.StringIO()
    usage_report.report(iter(rows), out=out)
    text = out.getvalue()
    day = next(l for l in text.splitlines() if l.startswith("2026-09-19"))
    cols = day.split()
    visitors, designers, downloads, _exports, refusals, failures, requests = map(int, cols[1:])
    assert visitors == 1, f"the bot fetched / with a bot UA and must not count:\n{text}"
    assert designers == 1, f"curl POSTing /outline is a script, not a designer:\n{text}"
    # Two: the person's, and the uploader's slow 200. An abusive download is
    # still a download; the watch lists are where it is told apart.
    assert downloads == 2, text
    assert "2 distinct addresses downloaded" in text, text
    assert refusals == 1, f"the 413 is a refusal; the scanner's 404s are not:\n{text}"
    assert failures == 0 and requests == len(LOG)
    # the watch lists name the right addresses for the right reasons
    assert SCANNER in text.split("404 probes")[1], text
    assert UPLOADER in text.split("bytes uploaded per address")[1].split("slower than")[0], text
    assert UPLOADER in text.split("slower than")[1], text
    refusal_list = text.split("refusals and failures per address")[1].split("watch list")[0]
    assert PERSON not in refusal_list, f"the person refused nothing:\n{text}"


def test_lines_that_are_not_access_log_are_skipped_not_fatal():
    junk = ["[2026-09-19 10:00:00 +0000] [1] [WARNING] minibadge_designer.webapp: x POST /generate -> 400: nope\n",
            "garbage\n", ""]
    assert list(usage_report.parse(junk)) == []
    out = io.StringIO()
    usage_report.report(iter([]), out=out)
    assert "no access-log lines matched" in out.getvalue()
