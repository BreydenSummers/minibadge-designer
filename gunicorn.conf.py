"""Gunicorn config for the container (CMD in the Dockerfile).

Worker processes, not threads, on purpose: every request already works in
its own TemporaryDirectory and shells out to its own kicad-cli, so isolated
processes make the remaining shared-state questions (font caches, the
refill-interpreter memo) disappear by construction, and CPU-bound shapely/
PIL work scales across cores instead of serializing on the GIL.
"""

import os

# 0.0.0.0 here is the *container's* network namespace, not the host: what is
# actually reachable is decided by the `ports` mapping in docker-compose.yml,
# which publishes on 127.0.0.1. Running this config directly on a host (no
# container) is the case that wants HOST=127.0.0.1.
bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8000')}"

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

# Recycle a worker after this many requests. Two reasons, both about memory
# rather than leaks: CPython returns large freed arenas to the OS only
# sometimes, so a worker that has once decoded a big image can sit at that high
# water mark indefinitely; and a fresh process is the cheapest way to be sure of
# that under a hard container memory limit (see docker-compose.yml). The jitter
# keeps four workers from retiring on the same request and emptying the pool.
max_requests = int(os.environ.get("MAX_REQUESTS", "200"))
max_requests_jitter = 40

# The reverse proxy on this host is the only thing that talks to us, and
# ProxyFix (webapp.py) reads the client address out of its X-Forwarded-For.
# Gunicorn has to be told which peer is allowed to set that header, or the
# access log keeps reporting the proxy's own loopback address for every hit --
# which is what made an abusive client unattributable in the logs. Override
# FORWARDED_ALLOW_IPS if the proxy ever moves off localhost.
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")

# The worker heartbeat file lives in memory; on containers with slow or
# throttled disk a /tmp heartbeat can miss and kill healthy workers.
# Linux-only path: running this config on macOS (dev) falls back to the
# default temp dir.
if os.path.isdir("/dev/shm"):
    worker_tmp_dir = "/dev/shm"

accesslog = "-"  # one line per request on stdout, where docker logs look

# The default access log format omits how long the request took, and duration
# is the interesting column here: the expensive endpoints are the ones worth
# watching, and "which requests are slow, from where" is the question an
# availability incident actually asks. %(D)s is microseconds; %(h)s is the real
# client address now that forwarded_allow_ips is set above.
# %({content-length}i)s is the upload size the client declared ("-" for none):
# it is what separates a person downloading a badge from an address feeding the
# app maximum-size images all afternoon, and the report script keys on it.
access_log_format = ('%(h)s "%(r)s" %(s)s %(b)s %(D)sus %({content-length}i)s "%(a)s"')


def worker_abort(worker):
    """Runs in the worker the arbiter is killing for exceeding `timeout`.

    Gunicorn's own line for that event is "WORKER TIMEOUT (pid:NNN)" and no
    more. The request being served gets no access line and no failure line,
    because the process dies here, so this is the only moment its address and
    URL can be written down. The app keeps them in a module global for exactly
    this reader (webapp._in_flight). Logged through gunicorn's error logger so
    it lands wherever WORKER TIMEOUT does: stderr and, with LOG_DIR, errors.log.
    """
    try:
        from minibadge_designer import webapp

        report = webapp.in_flight_report()
    except Exception as exc:  # noqa: BLE001 -- a dying worker must not die worse
        report = f"(could not read the in-flight request: {exc!r})"
    worker.log.critical("WORKER TIMEOUT killed the request in flight: %s",
                        report or "none; the worker was idle")

# ---- Where errors go --------------------------------------------------------
# The app logs one line per refused or failed request (webapp.py, the
# after_request hook) and gunicorn logs its own worker events (a WORKER TIMEOUT
# is the line that explains a user's hung export). Both go to stderr, which is
# `docker compose logs`, and additionally -- when LOG_DIR names a writable
# directory -- to a plain file there that the host can tail and grep without
# Docker. docker-compose.yml mounts the checkout's logs/ there by default.
#
# WatchedFileHandler, not RotatingFileHandler: four worker processes append to
# the same file, and only the former is safe to share across processes (each
# line is one O_APPEND write; it re-opens the file if something outside rotates
# or truncates it). Rotation is therefore the host's job, and the file only
# carries WARNING and up -- refusals, failures, worker deaths -- so it grows by
# the incident, not by the request. The access log stays on stdout only.
#
# logconfig_dict is merged shallowly over gunicorn's defaults, whose root logger
# has a stdout handler AND whose own loggers propagate to it, so every gunicorn
# line would print twice. Supplying both `loggers` entries with propagate off
# is what keeps each line single.
loglevel = os.environ.get("LOG_LEVEL", "info").lower()

_handlers = {
    "console": {"class": "logging.StreamHandler", "formatter": "generic",
                "stream": "ext://sys.stdout"},
    "error_console": {"class": "logging.StreamHandler", "formatter": "generic",
                      "stream": "ext://sys.stderr"},
}
_error_handlers = ["error_console"]
_access_handlers = ["console"]
_log_dir = os.environ.get("LOG_DIR")
if _log_dir:
    if os.path.isdir(_log_dir) and os.access(_log_dir, os.W_OK):
        _handlers["error_file"] = {
            "class": "logging.handlers.WatchedFileHandler",
            "formatter": "generic", "level": "WARNING",
            "filename": os.path.join(_log_dir, "errors.log"),
        }
        _error_handlers.append("error_file")
        # The access log too, as a file the host can keep for longer than
        # Docker's capped json-file and feed to scripts/usage_report.py: every
        # request, so it grows by traffic (about 150 bytes a line), and it is
        # what answers "how many people" and "who keeps hammering this".
        _handlers["access_file"] = {
            "class": "logging.handlers.WatchedFileHandler",
            "formatter": "generic",
            "filename": os.path.join(_log_dir, "access.log"),
        }
        _access_handlers.append("access_file")
    else:
        # Not fatal: a deploy must not fail because a directory is owned by
        # the wrong user. Said once on stderr, and the file simply is not kept.
        import sys

        print(f"gunicorn.conf.py: LOG_DIR={_log_dir!r} is not a writable "
              f"directory; errors go to stderr only. In the container it must "
              f"be writable by uid 1000: on the host, "
              f"`chown 1000 <LOG_DIR from .env, default ./logs>`.",
              file=sys.stderr)

logconfig_dict = {
    "formatters": {
        "generic": {
            "class": "logging.Formatter",
            "format": "%(asctime)s [%(process)d] [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "[%Y-%m-%d %H:%M:%S %z]",
        },
    },
    "handlers": _handlers,
    # The app's logger (minibadge_designer.webapp) has no handlers of its own
    # and inherits these from root; so does Flask's unhandled-exception
    # traceback, which goes through that same logger.
    "root": {"level": loglevel.upper(), "handlers": _error_handlers},
    "loggers": {
        "gunicorn.error": {"level": loglevel.upper(), "handlers": _error_handlers,
                           "propagate": False, "qualname": "gunicorn.error"},
        "gunicorn.access": {"level": "INFO", "handlers": _access_handlers,
                            "propagate": False, "qualname": "gunicorn.access"},
    },
}
