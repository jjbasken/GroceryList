import os

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
DATABASE = os.environ.get("DATABASE", "/data/grocery.db")
