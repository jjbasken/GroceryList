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
- **Admin panel** — user management, password resets, activation/deactivation, audit log
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
   ```

   Generate both secrets independently, for example with
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
pip install --require-hashes -r requirements.lock
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

Navigate to `/admin` (admin users only) to:

- Create, activate, deactivate, and delete users
- Force a password reset on next login
- View the audit log of all actions

## Security upgrade and offline storage

The account-identity migration runs automatically on startup. Existing users
must sign in again once after upgrading. Let connected devices finish syncing
before deploying: the updated service worker removes legacy offline caches and
queued changes because they cannot safely be assigned to an account.

Offline data belongs to the account that loaded the page. Logout, sign-in,
password changes, and account switches clear private browser caches and pending
changes. API writes require an `X-Account-ID` header matching the authenticated
account, in addition to the CSRF token. The frontend sets both automatically;
custom API clients can obtain the account identity from the `X-Account-ID`
response header on `/api/csrf-token`.

Run backend regressions with `python -m pytest -q` and browser-script regressions
with `node tests/offline-security.test.js` (Node.js 22 or newer).

Dependencies used by production and CI are fully resolved in hash-locked files.
After intentionally changing `requirements.txt` or `requirements-dev.txt`,
regenerate the corresponding `.lock` file with
`pip-compile --generate-hashes --allow-unsafe`. GitHub Actions runs tests,
dependency auditing, Bandit, a full-history secret scan, and a production
container build. Dependabot checks Python, Docker, and GitHub Actions inputs
weekly.

## Project Structure

```
app.py           # Flask application and all routes
config.py        # Environment-based configuration
schema.sql       # Database schema (initial creation)
requirements.txt
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
