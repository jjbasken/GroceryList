import os

SECRET_KEY = os.environ["SECRET_KEY"]   # raises KeyError if unset; no silent fallback
DATABASE = os.environ.get("DATABASE", "/data/grocery.db")
# Optional: enables "Import from URL" recipe extraction. The app runs fine
# without it -- that feature is just hidden/disabled when unset.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# No per-token expiry: tokens stay valid for the life of the session. The PWA
# can sit open for days between page renders, so a timed token would go stale
# and break writes until a manual refresh. Tokens are still session-bound.
WTF_CSRF_TIME_LIMIT = None
