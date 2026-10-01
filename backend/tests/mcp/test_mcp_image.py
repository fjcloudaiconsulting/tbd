"""TBD-561 F-M6 (pin half): the MCP image's requirements are a subset of the
backend's with the SAME pins. The build half is the CI ``mcp-image`` job,
which builds ``backend/Dockerfile.mcp`` and imports ``app.mcp_main`` in it.

Wrong implementations: a package pinned differently in the two files (the
component runs code tested against another version), a package the backend
does not carry at all, and an excluded package (alembic, ofxtools, pyotp,
qrcode) creeping back into the image.
"""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
EXCLUDED = {"alembic", "ofxtools", "pyotp", "qrcode", "python-multipart"}


def _pins(name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (BACKEND / name).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.-]+)(\[[^\]]+\])?\s*(.*)", line)
        assert m, f"unparsed requirement line {line!r} in {name}"
        out[m.group(1).lower()] = (m.group(2) or "") + m.group(3)
    return out


def test_fm6_mcp_requirements_are_a_same_pin_subset():
    full, mcp = _pins("requirements.txt"), _pins("requirements-mcp.txt")
    assert len(mcp) >= 10, "parsed too few mcp requirements; the parser is broken"
    missing = sorted(set(mcp) - set(full))
    assert not missing, f"not in requirements.txt: {missing}"
    drift = {k: (mcp[k], full[k]) for k in mcp if mcp[k] != full[k]}
    assert not drift, f"pins differ (mcp, backend): {drift}"
    assert all("==" in spec for spec in mcp.values()), mcp


def test_fm6_excluded_packages_stay_out():
    assert not EXCLUDED & set(_pins("requirements-mcp.txt"))


def test_fm6_dockerfile_copies_only_the_app_package():
    text = (BACKEND / "Dockerfile.mcp").read_text()
    copies = [ln.split()[1:] for ln in text.splitlines() if ln.startswith("COPY ")]
    assert copies == [["requirements-mcp.txt", "./"], ["app", "./app"]], copies
    assert '"app.mcp_main:app"' in text and "USER mcp" in text
