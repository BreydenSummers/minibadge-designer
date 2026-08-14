"""Docker entry point: serve the web UI on all interfaces."""

import os

from minibadge_designer.webapp import app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
