"""CLI entry point: serve the minibadge-designer web UI."""

from __future__ import annotations

import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="minibadge-designer",
        description="Web UI for designing minibadges (logo + LEDs -> KiCad project).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: %(default)s)")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode with auto-reload")
    args = parser.parse_args()

    # Same record the container keeps (see gunicorn.conf.py): one line per
    # refused or failed request, on stderr. Set before the app import so Flask
    # does not add a second handler of its own.
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    from .webapp import app

    print(f"minibadge-designer: open http://{args.host}:{args.port} in your browser")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
