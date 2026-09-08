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
| Backend | Python 3.12, Flask 3, gunicorn + gevent |
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
   # Optional — defaults to /data/grocery.db inside the container
   DATABASE=/data/grocery.db
   # Optional — only needed if using the Cloudflare tunnel service
   CLOUDFLARE_TUNNEL_TOKEN=your-token-here
   ```

2. Start the app:

   ```bash
   docker compose up -d
   ```

3. Open `http://localhost:5000` in your browser. The first user to register becomes the initial user; promote them to admin via the admin panel or directly in the database.

Data is persisted in the `./data/` directory on the host.

### Run Locally (no Docker)

```bash
pip install -r requirements.txt
export SECRET_KEY=your-secret-key
export DATABASE=grocery.db
python -c "import app; app.init_db(); app.upgrade_db()"
flask run
```

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | Yes | — | Flask session signing key |
| `DATABASE` | No | `/data/grocery.db` | Path to the SQLite database file |

## Cloudflare Tunnel

The `docker-compose.yml` includes an optional `tunnel` service that exposes the app via a Cloudflare Tunnel. Set `CLOUDFLARE_TUNNEL_TOKEN` in your `.env` to enable it. Remove or comment out the `tunnel` service block if you don't need it.

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
