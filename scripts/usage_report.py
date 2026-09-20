#!/usr/bin/env python3
"""Turn the access log into two answers: how many people, and who is misbehaving.

    python3 scripts/usage_report.py                    # logs/access.log, last 7 days
    python3 scripts/usage_report.py --since 30
    python3 scripts/usage_report.py logs/access.log logs/access.log.1
    docker compose logs --no-log-prefix | python3 scripts/usage_report.py -

Reads the lines gunicorn writes with the format in gunicorn.conf.py (the
compose-prefixed form from `docker compose logs` is accepted too) and prints:

  * per day: visitors, designers, downloads, exports, refusals, failures
  * totals for the window, and the busiest paths
  * a watch list: addresses ranked by refusals, by bytes uploaded, by slow
    requests, and by 404 probing -- the four shapes abuse takes here

"Visitor" is deliberately strict, because bots fetch "/" all day. An address
counts as a visitor on a day only if it fetched the page AND then did something
only the running editor does: POSTed to /outline (fires on every edit) or
fetched a /static/ asset. A "designer" went further and POSTed /outline at all;
a "download" is a 200 from POST /generate. These are counts of addresses, so a
household behind one NAT is one visitor and a person on two networks is two;
the number is an honest floor, not a headcount.

Nothing here talks to the network or writes anything.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

# The gunicorn "generic" formatter prefix, then the access_log_format fields.
# content-length is optional so lines from before it was added still parse.
LINE = re.compile(
    r"^(?:\S+\s+\|\s*)?"                               # compose prefix, if any
    r"\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4})\] \[\d+\] \[INFO\] "
    r"gunicorn\.access: (?P<ip>\S+) "
    r'"(?P<method>[A-Z]+) (?P<path>\S+)(?: [^"]*)?" '
    r"(?P<status>\d{3}) (?P<bytes>\S+) (?P<us>\d+)us "
    r"(?:(?P<clen>\S+) )?"
    r'"(?P<ua>[^"]*)"\s*$'
)

BOT_UA = re.compile(
    r"bot|crawl|spider|slurp|curl|wget|python-requests|python-urllib|go-http-client"
    r"|httpx|scrapy|nmap|masscan|zgrab|censys|nuclei|java/|libwww|^-?$",
    re.IGNORECASE,
)

#: Paths nobody reaches from the app; requesting them is scanning.
PROBE = re.compile(
    r"wp-|\.php|\.env|\.git|/admin|/phpmyadmin|/cgi-bin|\.aspx?$|/actuator"
    r"|/xmlrpc|/console|/vendor/|/\.well-known/(?!acme)",
    re.IGNORECASE,
)

SLOW_S = 20.0


def parse(lines):
    for raw in lines:
        m = LINE.match(raw.rstrip("\n"))
        if not m:
            continue
        d = m.groupdict()
        d["ts"] = datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S %z")
        d["status"] = int(d["status"])
        d["s"] = int(d["us"]) / 1e6
        d["clen"] = int(d["clen"]) if d["clen"] and d["clen"].isdigit() else 0
        d["route"] = d["path"].split("?", 1)[0]
        d["bot"] = bool(BOT_UA.search(d["ua"]))
        yield d


def report(rows, out=sys.stdout):
    days = defaultdict(lambda: {
        "page": set(), "editor": set(), "designers": set(), "downloads": 0,
        "downloaders": set(), "exports": 0, "refusals": 0, "failures": 0,
        "requests": 0})
    paths = Counter()
    refusals = Counter(); uploaded = Counter(); slow = Counter(); probes = Counter()
    ua_of = {}
    n = 0
    for r in rows:
        n += 1
        day = days[r["ts"].date()]
        day["requests"] += 1
        ip, route, st = r["ip"], r["route"], r["status"]
        paths[route] += 1
        ua_of[ip] = r["ua"]
        if not r["bot"]:
            if route == "/" and st == 200 and r["method"] == "GET":
                day["page"].add(ip)
            if route == "/outline" or route.startswith("/static/"):
                day["editor"].add(ip)
            if route == "/outline" and r["method"] == "POST":
                day["designers"].add(ip)
            if route == "/generate" and r["method"] == "POST" and st == 200:
                day["downloads"] += 1
                day["downloaders"].add(ip)
            if route in ("/gerbers", "/model3d") and st == 200:
                day["exports"] += 1
        if 400 <= st < 500:
            if st == 404 and PROBE.search(r["path"]):
                probes[ip] += 1
            elif st != 404:
                day["refusals"] += 1
                refusals[ip] += 1
        elif st >= 500:
            day["failures"] += 1
            refusals[ip] += 1
        uploaded[ip] += r["clen"]
        if r["s"] > SLOW_S:
            slow[ip] += 1

    if n == 0:
        print("no access-log lines matched; is this gunicorn's access log?", file=out)
        return

    w = out.write
    w(f"{'day':<12}{'visitors':>9}{'designers':>10}{'downloads':>10}"
      f"{'exports':>8}{'refusals':>9}{'failures':>9}{'requests':>9}\n")
    tot = Counter(); all_visitors = set(); all_designers = set(); all_dl = set()
    for d in sorted(days):
        x = days[d]
        visitors = x["page"] & x["editor"]
        all_visitors |= visitors; all_designers |= x["designers"]; all_dl |= x["downloaders"]
        for k in ("downloads", "exports", "refusals", "failures", "requests"):
            tot[k] += x[k]
        w(f"{d.isoformat():<12}{len(visitors):>9}{len(x['designers']):>10}"
          f"{x['downloads']:>10}{x['exports']:>8}{x['refusals']:>9}"
          f"{x['failures']:>9}{x['requests']:>9}\n")
    w(f"{'total':<12}{len(all_visitors):>9}{len(all_designers):>10}"
      f"{tot['downloads']:>10}{tot['exports']:>8}{tot['refusals']:>9}"
      f"{tot['failures']:>9}{tot['requests']:>9}\n")
    w(f"\n{len(all_dl)} distinct addresses downloaded a badge; "
      f"visitors are distinct addresses per day, so the total is of the union.\n")

    w("\nbusiest paths\n")
    for route, c in paths.most_common(8):
        w(f"  {c:>7}  {route}\n")

    def watch(title, counter, fmt=lambda v: str(v)):
        top = [(ip, v) for ip, v in counter.most_common(10) if v]
        if not top:
            return
        w(f"\n{title}\n")
        for ip, v in top:
            w(f"  {fmt(v):>10}  {ip:<40} {ua_of.get(ip, '')[:50]}\n")

    watch("watch list: refusals and failures per address", refusals)
    watch("watch list: bytes uploaded per address", uploaded,
          lambda v: f"{v / 1e6:.1f} MB" if v >= 1e6 else f"{v / 1e3:.0f} kB")
    watch(f"watch list: requests slower than {SLOW_S:.0f}s per address", slow)
    watch("watch list: 404 probes for admin/php/env paths per address", probes)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*", default=["logs/access.log"],
                    help="access log files, or - for stdin (default logs/access.log)")
    ap.add_argument("--since", type=float, default=7,
                    help="only the last N days (default 7; 0 = everything)")
    args = ap.parse_args(argv)

    def lines():
        for f in args.files:
            if f == "-":
                yield from sys.stdin
            else:
                with Path(f).open(encoding="utf-8", errors="replace") as fh:
                    yield from fh

    rows = parse(lines())
    if args.since:
        cutoff = datetime.now(UTC) - timedelta(days=args.since)
        rows = (r for r in rows if r["ts"] >= cutoff)
    report(rows)


if __name__ == "__main__":
    main()
