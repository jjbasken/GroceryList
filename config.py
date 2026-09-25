import os

SECRET_KEY = os.environ["SECRET_KEY"]   # raises KeyError if unset; no silent fallback
DATABASE = os.environ.get("DATABASE", "/data/grocery.db")
BOOTSTRAP_TOKEN = os.environ.get("BOOTSTRAP_TOKEN", "")
ENFORCE_HTTPS = os.environ.get("ENFORCE_HTTPS", "false").lower() in ("1", "true", "yes")
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "false").lower() in ("1", "true", "yes")
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))
# No per-token expiry: tokens stay valid for the life of the session. The PWA
# can sit open for days between page renders, so a timed token would go stale
# and break writes until a manual refresh. Tokens are still session-bound.
WTF_CSRF_TIME_LIMIT = None
