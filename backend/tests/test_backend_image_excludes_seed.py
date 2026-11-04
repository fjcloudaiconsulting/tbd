"""INF-11: the published backend images do not ship ``seed.py``.

``seed.py`` creates ``demo / demo1234``, and on an empty database the first account registered
becomes a verified superadmin, so a copy inside a production image is one ``python seed.py``
away from a known-password superadmin. It is kept out of the build context, which covers the
prod and migrations targets alike (neither runs it). Dev keeps it through docker-compose.yml's
bind mount, which ``./tbd seed`` and the seed tests read.
"""

from __future__ import annotations

from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parents[1]


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    raise RuntimeError("Could not locate the repo root containing .github/workflows/ci.yml.")


def test_the_build_context_excludes_seed_py_and_dev_still_mounts_it():
    patterns = [
        line.strip()
        for line in (BACKEND / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "seed.py" in patterns
    # A later "!seed.py" would re-include it.
    assert not any(p.startswith("!") and p.lstrip("!/") == "seed.py" for p in patterns)

    # ./tbd seed and the seed tests read the bind-mounted copy.
    compose = yaml.safe_load(
        (_find_repo_root(Path(__file__).resolve()) / "docker-compose.yml").read_text(encoding="utf-8")
    )
    assert "./backend/seed.py:/app/seed.py" in compose["services"]["backend"]["volumes"]
