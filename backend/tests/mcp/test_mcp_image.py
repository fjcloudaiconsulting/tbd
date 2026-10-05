"""TBD-561 F-M6 (pin half): the MCP image's dependencies are a subset of the
backend's with the SAME pins. The build half is the CI ``mcp-image`` job,
which builds ``backend/Dockerfile.mcp`` and imports ``app.mcp_main`` in it.

Wrong implementations: a package pinned differently in the two files (the
component runs code tested against another version), a package the backend
does not carry at all, and an excluded package (alembic, ofxtools, pyotp,
qrcode) creeping back into the image.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
EXCLUDED = {"alembic", "ofxtools", "pyotp", "qrcode", "python-multipart"}


def _pins(group: str) -> dict[str, str]:
    out: dict[str, str] = {}
    groups = tomllib.loads((BACKEND / "pyproject.toml").read_text())["dependency-groups"]
    for req in groups[group]:
        m = re.fullmatch(r"([A-Za-z0-9_.-]+)(\[[^\]]+\])?(.*)", req)
        out[m.group(1).lower()] = (m.group(2) or "") + m.group(3)
    return out


def test_fm6_mcp_requirements_are_a_same_pin_subset():
    full, mcp = _pins("app"), _pins("mcp")
    assert len(mcp) >= 10, "parsed too few mcp requirements; the parser is broken"
    missing = sorted(set(mcp) - set(full))
    assert not missing, f"not in the app group: {missing}"
    drift = {k: (mcp[k], full[k]) for k in mcp if mcp[k] != full[k]}
    assert not drift, f"pins differ (mcp, backend): {drift}"
    assert all("==" in spec for spec in mcp.values()), mcp


def test_fm6_excluded_packages_stay_out():
    assert not EXCLUDED & set(_pins("mcp"))


def test_fm6_dockerfile_copies_only_the_app_package():
    text = (BACKEND / "Dockerfile.mcp").read_text()
    # Renovate bumps the uv tag and pins ``repo:x.y.z@sha256:<64 hex>``; the
    # repo stays asserted, any x.y.z tag and an optional digest are accepted.
    copies = [
        [re.sub(r"^(--from=ghcr\.io/astral-sh/uv):\d+\.\d+\.\d+(@sha256:[0-9a-f]{64})?$", r"\1", tok) for tok in ln.split()[1:]]
        for ln in text.splitlines()
        # Instructions are case-insensitive and may be indented; ADD copies too.
        if re.match(r"\s*(COPY|ADD)\s", ln, re.IGNORECASE)
    ]
    assert copies == [["--from=ghcr.io/astral-sh/uv", "/uv", "/uvx", "/bin/"], ["pyproject.toml", "uv.lock", "./"], ["app", "./app"]], copies
    assert "--no-default-groups --group mcp" in text
    assert '"app.mcp_main:app"' in text and "USER mcp" in text
