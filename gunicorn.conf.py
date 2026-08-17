"""Gunicorn config for the container (CMD in the Dockerfile).

Worker processes, not threads, on purpose: every request already works in
its own TemporaryDirectory and shells out to its own kicad-cli, so isolated
processes make the remaining shared-state questions (font caches, the
refill-interpreter memo) disappear by construction, and CPU-bound shapely/
PIL work scales across cores instead of serializing on the GIL.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# 4 sync workers ≈ 4 users generating at the same instant; later arrivals
# queue in the socket backlog rather than failing. Override per host with
# WEB_CONCURRENCY (2 × cores is a sane ceiling; each worker is ~100 MB).
workers = int(os.environ.get("WEB_CONCURRENCY", "4"))
threads = 1

# A worker is legitimately busy for minutes on the heaviest boards: the GLB
# export and the zone refill each carry a 120 s subprocess budget. The same
# timeout is also the backstop that turns a pathological request (a huge
# SVG path is quadratic in the art pipeline) into a killed-and-restarted
# worker instead of a permanently lost one.
timeout = int(os.environ.get("WORKER_TIMEOUT", "300"))
graceful_timeout = 30

# The worker heartbeat file lives in memory; on containers with slow or
# throttled disk a /tmp heartbeat can miss and kill healthy workers.
# Linux-only path: running this config on macOS (dev) falls back to the
# default temp dir.
if os.path.isdir("/dev/shm"):
    worker_tmp_dir = "/dev/shm"

accesslog = "-"  # one line per request on stdout, where docker logs look
