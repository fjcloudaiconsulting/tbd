#!/usr/bin/env python3
"""Regenerate frontend/tests/fixtures/feature-catalog.json.

Reads ALL_FEATURE_KEYS from backend/app/auth/feature_catalog.py and
writes a sorted JSON list to the fixture used by the frontend drift
test. Run after adding or removing a feature key.

This is explicit (manual) — never invoked from a test.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.auth.feature_catalog import ALL_FEATURE_KEYS  # noqa: E402

FIXTURE = ROOT / "frontend" / "tests" / "fixtures" / "feature-catalog.json"

payload = {"keys": sorted(ALL_FEATURE_KEYS)}
FIXTURE.write_text(json.dumps(payload, indent=2) + "\n")
print(f"wrote {FIXTURE} with {len(payload['keys'])} keys")
