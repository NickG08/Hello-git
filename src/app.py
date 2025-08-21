import os

DB_URL = os.getenv("DB_URL", "sqlite:///./local.db")
print(f"Connected to: {DB_URL}")
