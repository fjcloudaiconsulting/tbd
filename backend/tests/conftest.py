import logging
import os
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The app settings module validates JWT_SECRET_KEY at import time.
# Tests set a stable secret up front so importing app modules does not
# depend on an external .env file being present in the worktree.
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "test-jwt-secret-that-is-long-enough-for-pytest-1234567890",
)
os.environ.setdefault("APP_ENV", "development")

# Match the production logging.py suppression: ofxtools emits per-row INFO
# during OFX parses ("Converting <STMTTRN>"). For tests that parse the
# 10k-row fixture this distorts wall-clock timing AND floods captured
# log output. Apply the same WARNING floor at conftest import so it
# takes effect before any test session-level fixture imports parser
# modules.
logging.getLogger("ofxtools").setLevel(logging.WARNING)
