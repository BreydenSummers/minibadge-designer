# Operating minibadge-designer

The long version: what the Docker build does, how the container is tuned and
contained, where the logs go, and how the site deploys. The README covers
getting it running; this covers keeping it running.

## What the build does

1. Pulls KiCad 9.x from Debian and keeps **only the ten 3D models** this app
   can place; the stock library is ~5 GB, this is half a megabyte. The result
   is ~800 MB rather than the 6.3 GB of the official `kicad/kicad:9.0-full`.
2. Fetches the third-party runtime assets (typefaces, `<model-viewer>`) by
   pinned version and SHA-256; see [Third-party assets](#third-party-assets).
3. Runs a **smoke test**: it exports a two-LED board and fails the build if
   the component models did not resolve or lost their colours. Every way this
   has broken before was silent, producing a perfectly valid export of an
   empty board, so the build refuses to ship one.

The container runs as a non-root user and reports health to Compose. A `.env`
file is picked up if present but is not required.

## Serving more than one person

The container serves through **gunicorn with 4 single-threaded worker
processes** (`gunicorn.conf.py`), so simultaneous users get parallel workers
rather than queueing behind one interpreter. Process isolation is also what
makes concurrent exports safe by construction, since every request works in
its own temp directory with its own kicad-cli. Tune per host via `.env`:

- `WEB_CONCURRENCY`: worker count (default 4; ~100 MB each, 2 × cores is a
  sane ceiling)
- `WORKER_TIMEOUT`: per-request ceiling in seconds (default 300; the GLB
  export and zone refill each carry a 120 s subprocess budget, and the
  timeout doubles as the backstop that recycles a worker stuck on a
  pathological upload)
- `PORT`: bind port inside the container (default 8000)
- `HOST`: bind address inside the container (default `0.0.0.0`, which is
  the container's own namespace — what the outside world can reach is set
  by the `ports` mapping above, not by this)
- `MAX_REQUESTS`: requests a worker serves before it is recycled (default
  `200`) — memory hygiene under the container's hard limit, not a leak fix
- `EXPORT_BUDGET_S`: wall clock one `/gerbers` or `/model3d` request may spend
  in subprocesses (default `240`). Keep it comfortably under `WORKER_TIMEOUT`,
  or a slow export is killed before it can report that it was slow.
- `FORWARDED_ALLOW_IPS`: which peer gunicorn accepts forwarded headers from
  (default `127.0.0.1`, the reverse proxy on this host)

The visitor's address comes from Cloudflare's `CF-Connecting-IP`, not from
counting positions in `X-Forwarded-For`. Counting does not work here: the local
proxy's two usual configurations either append its peer (making the rightmost
entry Cloudflare's edge) or overwrite the header outright (losing the visitor
entirely), and both were measured resolving to the edge address rather than the
person. `CF-Connecting-IP` is a single value that Cloudflare overwrites on every
request, so there is no chain to count.

That is only sound while the app is unreachable except through Cloudflare, which
is what the loopback-only `ports` mapping buys. **If the origin is ever exposed
directly, the header becomes forgeable** and the check has to become "is the peer
a Cloudflare address".

## Logs

Two records, for two questions.

- **What is happening?** `docker compose logs -f`. Every request is one line on
  stdout (client, request, status, bytes, duration in µs, user agent); gunicorn's
  own events and the app's failures are on stderr. Docker keeps the last 5 × 20 MB
  of it (`logging:` in `docker-compose.yml`), so a scanner cannot fill the disk.
- **What went wrong?** `tail -f logs/errors.log` in the checkout, no Docker
  needed. One line per refused (4xx, `WARNING`) or failed (5xx, `ERROR`) request:
  the client, the endpoint, the status, the message the user saw, and where the
  handler knew more than it said, the cause — the exception behind a "could not
  process the board shape", or kicad-cli's full stderr behind a "KiCad could not
  export this board". Worker timeouts and crashes from gunicorn land there too.
  The HTML 404s bots generate are left out on purpose.

The file lives in the checkout's own `logs/` by default because that directory
exists wherever the code does and belongs to whoever cloned it. To put it
somewhere else, set `LOG_DIR` in `.env` to a directory the container's uid 1000
can write:

```bash
sudo mkdir -p /var/log/minibadge && sudo chown 1000 /var/log/minibadge
echo LOG_DIR=/var/log/minibadge >> .env
```

If the directory turns out not to be writable, the container still starts: it
says so once on stderr and keeps the errors on stderr only, so a wrong `LOG_DIR`
costs the file, never the site. `LOG_LEVEL` (default `info`) sets both gunicorn's
and the app's verbosity; the file itself only ever takes `WARNING` and up, so it
grows by the incident, not by the request. Rotation is the host's job; a
`logrotate` stanza that works with the shared append handler the workers use
(no `copytruncate`, no restart):

```
/opt/minibadge-designer/logs/errors.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```

The dev server (`python3 main.py`, `minibadge-designer`) writes the same failure
lines to stderr and never to a file.

## Limits on what one request can spend

Every route is unauthenticated and decodes a file the caller chose, so each of
these has a ceiling. All are documented in place with the measurement that set
them; the numbers to know are:

| Ceiling | Where | Refuses |
| --- | --- | --- |
| 24 megapixels per upload | `logo.MAX_INPUT_PIXELS` | Read off the header, before a row is decoded — the only place a memory guard can stand, since PNG decoding is all-or-nothing |
| 4 megapixels working size | `logo.WORK_MAX_PIXELS` | The reduction happens before the RGBA conversion and the rotate, so peak memory tracks the badge and not the file |
| 40 000 weighted SVG segments | `webapp.MAX_SVG_COMPLEXITY` | Scored on the *render tree* including `<use>` expansion, so a 1 KB file that expands to 130 000 shapes is priced as 130 000 |
| 6 000 outline rectangles | `webapp.MAX_OUTLINE_RECTS` | One allowance shared by all twelve outline elements. Set at the measured knee: `/outline` runs on every edit |
| 24 MiB per request | `webapp.MAX_UPLOAD` | Flask `MAX_CONTENT_LENGTH` |
| 3 GB / 2 CPU / 512 pids | `docker-compose.yml` | The container, so the host is never the thing that runs out |

The container also runs read-only (`/tmp`, `/dev/shm` and `/home/badge` are
tmpfs — kicad-cli needs a writable `HOME`), as a non-root user, with
`no-new-privileges`.

Two things the app cannot do for itself, both at the edge: the
`http://` → `https://` redirect and rate limiting. The security headers,
including HSTS, are sent from the origin so they survive a change of provider.

## Branches, CI, and deploying

`dev` is the working branch; `main` is the deploy branch. Merging (or pushing)
to `main` is what ships.

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `.github/workflows/ci.yml` | push to `dev`, PRs into `dev`/`main` | Builds the image (whose last stage smoke-tests the 3D pipeline) and runs the test suite *inside* it, so tests see the same kicad-cli and build-time assets the container serves with. |
| `.github/workflows/deploy.yml` | push to `main`, manual dispatch | On the deploy host: fast-forwards `/opt/minibadge-designer` to the pushed commit, `docker compose up -d --build --wait`, and fails the job if the healthcheck never passes. |

The two run on different machines, on purpose. **CI runs on a GitHub-hosted
runner (`ubuntu-latest`); only the deploy runs on the self-hosted runner `badge`
(`self-hosted, Linux, X64`), which is also the server.**

Both used to run on `badge`, so that CI's build warmed the layer cache the
deploy reuses. That was the sharpest edge in the setup: a self-hosted runner
executes whatever the commit it checked out says to execute, on the machine that
serves the site, and the runner user is in the `docker` group — which is
root-equivalent on the host. Every commit that reached CI therefore had a route
to the production box that did not go through `deploy.yml` at all. Splitting CI
onto a disposable VM closes that route: the only thing that runs on `badge` now
is a push to `main`.

The cost is the warm cache. Deploys still reuse the layers left by the previous
deploy, so it is only felt when a change lands early in the `Dockerfile` —
`requirements.lock`, or either apt stage.

CI is still gated to **same-repository commits only**: a pull request from a
fork is skipped, not built (see the `if:` on the job in `ci.yml`). On a hosted
runner that is no longer a security boundary — the VM is disposable, the token
is read-only, and no secrets reach it — so it is now just a guard on runner
minutes, and it can be dropped if you want fork PRs to test themselves.

Two things CI deliberately does not cover: the **browser tier** (no Chromium in
the image; run `pytest -m browser` locally) and the **visual tier**, which
never gates anywhere because renders are not deterministic at the artifact
level. Run the full local suite before merging to `main`.

To roll back, point the deploy clone at the previous commit and bring it up:

```bash
cd /opt/minibadge-designer
git reset --hard <previous-sha>
docker compose up -d --build --wait
```

## Third-party assets

This repository contains no third-party files. The typefaces and the
`<model-viewer>` web component belong to other projects under other licences,
so they are downloaded on setup instead of being committed here:

```bash
python3 scripts/fetch_assets.py            # fetch (Docker does this for you)
python3 scripts/fetch_assets.py --check    # verify what is installed
```

Every asset is pinned to an exact version and verified against a SHA-256, so a
re-fetch cannot silently change a glyph; a mismatch aborts rather than
installing. The script records what it fetched, and under which licence, in
`minibadge_designer/fonts/THIRD-PARTY-NOTICE.txt`:

| Asset | Source | Licence |
|---|---|---|
| 11 display typefaces | [google/fonts](https://github.com/google/fonts) (pinned commit) | SIL Open Font License 1.1 |
| Special Elite | [google/fonts](https://github.com/google/fonts) (pinned commit) | Apache License 2.0 |
| `<model-viewer>` 4.0.0 | [google/model-viewer](https://github.com/google/model-viewer) | BSD 3-Clause |

KiCad's 3D model libraries are likewise never vendored: the Docker image
installs them from Debian at build time (CC-BY-SA-4.0 with the KiCad library
exception).
