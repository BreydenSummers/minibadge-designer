"""Fallback entry point: the Flask dev server on the loopback interface.

The container no longer uses this; its CMD is gunicorn with worker
processes (see gunicorn.conf.py), because the dev server handles requests
one at a time per thread and two users exporting at once would queue.
Kept for `python3 main.py` quick runs where that trade-off is fine.
"""

import logging
import os

# Before the app import: Flask attaches its own stderr handler to the app
# logger only if nothing above it has one, and this is what gives the dev
# server the same one-line-per-failure record the container writes.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from minibadge_designer.webapp import app

if __name__ == "__main__":
    # Loopback by default: the dev server has no auth and this process is
    # usually someone's laptop. Set HOST=0.0.0.0 to serve the network on purpose.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")))
