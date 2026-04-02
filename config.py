import os

SECRET_KEY = os.environ["SECRET_KEY"]   # raises KeyError if unset; no silent fallback
DATABASE = os.environ.get("DATABASE", "/data/grocery.db")
WTF_CSRF_TIME_LIMIT = None  # Token valid for session lifetime; needed for offline sync
