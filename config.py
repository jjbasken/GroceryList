import os

SECRET_KEY = os.environ["SECRET_KEY"]   # raises KeyError if unset; no silent fallback
DATABASE = os.environ.get("DATABASE", "/data/grocery.db")
WTF_CSRF_TIME_LIMIT = 86400  # 24 h; queue entries store their own token so offline sync survives rotation
