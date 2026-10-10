# GroceryList

A self-hosted, multi-user grocery list web app built with Flask. Items are organized into **Now** and **Later** sections, real-time updates are pushed to all connected clients via SSE, and the app works offline via a service worker with an IndexedDB sync queue.

## Features

- **Multi-user** with role-based access (user / admin)
- **Multiple lists** — create and switch between named lists
- **Two sections per list** — "Now" (active) and "Later" (saved for later)
- **Item details** — optional quantity and notes per item
- **Autocomplete** — item name history for quick re-entry
- **Real-time sync** — Server-Sent Events push changes to all open tabs/clients instantly
- **Offline support** — service worker caches the app shell; queued writes sync when connectivity returns
- **Admin panel** — user management, password resets, activation/deactivation, session revocation, audit log
- **External API** — optional token-authenticated endpoints so companion apps (e.g. [MenuPlanner](https://github.com/jjbasken/MenuPlanner)) can push items onto a list
- **Security** — CSRF protection, bcrypt passwords, rate limiting, strict CSP headers, HttpOnly cookies

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.14, Flask 3, gunicorn + gevent |
| Database | SQLite (WAL mode) |
| Frontend | Vanilla JS, CSS, PWA service worker |
| Container | Docker + Docker Compose |
| Tunnel | Cloudflare Tunnel (optional) |

## Quick Start

### Prerequisites

- Docker and Docker Compose

### Run with Docker Compose

1. Clone the repo and create a `.env` file:

   ```
   SECRET_KEY=your-random-secret-key-here
   # Required only until the initial administrator has been created
   BOOTSTRAP_TOKEN=your-random-one-time-setup-token
   # Optional — defaults to /data/grocery.db inside the container
   DATABASE=/data/grocery.db
   # Optional — only needed if using the Cloudflare tunnel service
   CLOUDFLARE_TUNNEL_TOKEN=your-token-here
   # Linux bind-mount ownership (use `id -u` and `id -g`)
   APP_UID=1000
   APP_GID=1000
   # Optional — enables the /api/external/* endpoints (see "External API")
   EXTERNAL_API_TOKEN=
   ```

   Generate each secret independently, for example with
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

2. Start the app:

   ```bash
   mkdir -p data
   docker compose up -d
   ```

   If an existing `data/` directory was created by a root-running version of
   the container, change its ownership to the `APP_UID`/`APP_GID` configured
   above before starting the hardened non-root image.

3. Open the HTTPS hostname configured for your Cloudflare tunnel. Enter the
   one-time setup token to create the initial administrator, then remove
   `BOOTSTRAP_TOKEN` from `.env` and restart the services.

The production Compose configuration trusts one reverse-proxy hop and redirects
HTTP requests to HTTPS. For loopback-only development without the tunnel, set
`ENFORCE_HTTPS=false` and `TRUST_PROXY_HEADERS=false` in `.env`, then use
`http://localhost:5000`.

Data is persisted in the `./data/` directory on the host.

### Run Locally (no Docker)

```bash
pip install --require-hashes -r requirements.txt
export SECRET_KEY=your-secret-key
export BOOTSTRAP_TOKEN=your-one-time-setup-token
export DATABASE=grocery.db
python -c "import app; app.init_db(); app.upgrade_db()"
flask run
```

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | Yes | — | Flask session signing key |
| `BOOTSTRAP_TOKEN` | Initial setup | — | One-time token required to create the first administrator |
| `DATABASE` | No | `/data/grocery.db` | Path to the SQLite database file |
| `ENFORCE_HTTPS` | No | `false` | Redirect HTTP to HTTPS; Compose defaults this to `true` |
| `TRUST_PROXY_HEADERS` | No | `false` | Trust proxy client/scheme headers; enable only behind the configured tunnel |
| `TRUSTED_PROXY_HOPS` | No | `1` | Number of trusted proxy hops |
| `EXTERNAL_API_TOKEN` | No | — | Shared secret for `/api/external/*`; when unset those endpoints return 404 |

## Cloudflare Tunnel

The `docker-compose.yml` includes an optional `tunnel` service that exposes the
app via a Cloudflare Tunnel. Set `CLOUDFLARE_TUNNEL_TOKEN` in your `.env` to
enable it. Remove or comment out the tunnel service and disable the two proxy/TLS
settings for loopback-only HTTP development. The application emits HSTS on
HTTPS responses; Cloudflare should also have **Always Use HTTPS** enabled.

The container health check calls `/healthz`, which reports whether the app can
reach its database. It needs no login, works before initial setup, and is exempt
from the HTTPS redirect so it can be called over loopback HTTP.

## Admin Panel

Navigate to `/admin/users` (admin users only) to:

- Create, activate, deactivate, and delete users
- Reset a user's password (they must change it on next login)
- Revoke all of a user's sessions
- View the 200 most recent audit log entries

## External API

Companion apps can add items without a browser session by sending a bearer token.
Generate one (e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`),
set it as `EXTERNAL_API_TOKEN`, and give the same value to the companion app.
These endpoints don't use cookies, so they're exempt from CSRF and `X-Account-ID`
checks.

`GET /api/external/lists` → `[{"id": 1, "name": "Groceries"}, ...]`

`POST /api/external/items`

```json
{
  "list_id": 1,
  "merge": true,
  "items": [
    {"name": "Chicken breast", "quantity": "2 lb", "notes": "for Fajitas", "section": "now"}
  ]
}
```

- `list_id` is optional and defaults to the first list. An unknown id returns 400.
- A request can carry up to 100 items. `name` is required. `quantity` (≤50 chars), `notes` (≤500) and `section` (`now`/`later`, default `now`) are optional. If any item is invalid, nothing is added.
- With `merge: true`, an item with the same name as an un-bought item on the same list (case-insensitive) doesn't create a duplicate. Its quantity and notes are appended to the existing item's notes instead.
- A successful request returns 201 with `{"added": [...items], "merged": [...items]}`. Connected clients refresh immediately via SSE, and the request is recorded in the audit log.

Containers on the same Docker host can call `http://web:5000` directly by joining
the `grocery-net` network, which has a fixed name in `docker-compose.yml`. Direct
calls arrive without `X-Forwarded-Proto`, so they're exempt from the HTTPS
redirect. A plain-HTTP request that comes in through the tunnel is still
redirected.

## Security upgrade and offline storage

The account-identity migration runs automatically on startup. Existing users
must sign in again once after upgrading. Let connected devices finish syncing
before deploying: the updated service worker removes legacy offline caches and
queued changes because they cannot safely be assigned to an account.

Offline sync requires a current browser with Web Locks support (current Chrome,
Edge, Firefox, and Safari). Queue replay is serialized across tabs, temporary
server errors and rate limits retain pending writes for retry, and app-shell
updates preserve current account caches and queued changes. Reopening the app
or switching lists reconstructs pending changes over the cached server data.

Offline data belongs to the account that loaded the page. Logout, sign-in,
password changes, and account switches clear private browser caches and pending
changes. API writes require an `X-Account-ID` header matching the authenticated
account, in addition to the CSRF token. The frontend sets both automatically;
custom API clients can obtain the account identity from the `X-Account-ID`
response header on `/api/csrf-token`.

Run backend regressions with `python -m pytest -q` and browser-script regressions
with `node tests/offline-security.test.js` (Node.js 22 or newer).

Dependencies used by production and CI are fully resolved in hash-locked files.
Direct dependencies are declared in `requirements.in` and `requirements-dev.in`;
`requirements.txt` and `requirements-dev.txt` are generated from them. After
changing an `.in` file, regenerate its `.txt` with
`pip-compile --generate-hashes --allow-unsafe --strip-extras <file>.in`
(Dependabot does this automatically for its update PRs). GitHub Actions runs tests,
dependency auditing, Bandit, a full-history secret scan, and a production
container build. Dependabot checks Python, Docker, and GitHub Actions inputs
weekly.

## Pull requests and security

The default branch is currently `master`. Repository rules protect both `master`
and `main` (and follow the default branch if it changes). Changes require a pull
request, passing security checks, and resolved review conversations. Direct pushes,
force pushes, and branch deletion are blocked. These core protections have no
bypass actors.

A separate native GitHub review ruleset requires one approving review from a code
owner listed in `.github/CODEOWNERS` (currently `jjbasken`). GitHub does not let an
author approve their own PR review, so `jjbasken` alone can bypass this **review
ruleset through a PR**. When merging your own PR, use GitHub's review-bypass option;
you do not need a second reviewer or a separate deployment approval. This bypass
does not waive the PR requirement, security checks, or other core protections.
Other contributors must obtain a code owner's approval.

To add approvers, update `.github/CODEOWNERS` and keep Dependabot's review requests
aligned. The owner-only bypass is configured separately in the repository's
**Settings → Rules → Rulesets**, and applies only to the review ruleset.

Dependabot checks Python dependencies, container images, and GitHub Actions weekly
and opens security update PRs. GitHub secret scanning and push protection scan for
leaked credentials. CodeQL default setup scans Python, JavaScript, and Actions;
the branch rules block new high/critical security findings and error-level code
scanning findings. CI also runs pip-audit, Bandit, Gitleaks, backend and browser
regressions, a container build, and dependency review on PRs. Scheduled security
checks run weekly so existing dependencies continue to be audited.

## Project Structure

```
app.py           # Flask application and all routes
config.py        # Environment-based configuration
conftest.py      # pytest fixtures
schema.sql       # Database schema (initial creation)
requirements.in   # Direct dependencies
requirements.txt  # Hash-locked, generated by pip-compile
requirements-dev.in / requirements-dev.txt  # Test/CI tooling
tests/           # pytest suite + offline security JS test
Dockerfile
docker-compose.yml
static/
  app.js         # Main frontend logic
  sw.js          # Service worker (offline + sync queue)
  style.css
  manifest.webmanifest
templates/       # Jinja2 HTML templates
```

## License

[GNU General Public License v3.0](LICENSE)
