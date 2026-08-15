"""Fallback entry point: the Flask dev server on all interfaces.

The container no longer uses this — its CMD is gunicorn with worker
processes (see gunicorn.conf.py), because the dev server handles requests
one at a time per thread and two users exporting at once would queue.
Kept for `python3 main.py` quick runs where that trade-off is fine.
"""

import os

from minibadge_designer.webapp import app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
