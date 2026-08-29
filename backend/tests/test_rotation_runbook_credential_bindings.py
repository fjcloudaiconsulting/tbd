"""The runbooks must enumerate every data-plane credential binding.

WHY THIS EXISTS. `.do/app.yaml` declares the `DATABASE_URL` / `REDIS_URL`
bindings an operator has to re-encrypt by hand during a credential rotation or a
cutover. `infra/MIGRATION.md` tells them how many there are. That count has gone
stale TWICE -- the cutover's step 6 table and step 9's diff list both said THREE
from 2026-05 until 2026-08-28, because `jobs.migrate` gained its own `REDIS_URL`
on 2026-08-20 and nothing made the prose follow.

Missing a binding is SILENT in both directions. `backend/scripts/migrate.py`
never touches Redis and `Settings.redis_url` defaults to `""`, so nothing fails
at deploy time; and `scripts/ci/assert-app-spec-secrets-synced.sh` compares
committed against LIVE, so a value left stale in both still matches and passes
that guard. The operator finishes believing the rotation is complete, with a
dead credential in the spec.

The runbook already says "do not take that count on trust". This makes that
executable: add or remove a data-plane binding in `.do/app.yaml` and these go
red, naming the runbook as the thing to update.

⚠ These parse the spec with YAML rather than grepping it, and
`test_the_spec_parser_ignores_a_comment_and_sees_a_real_binding` proves that in
BOTH directions. A grep over `.do/app.yaml` is satisfied by the file's own
comments, which quote these very key names in prose (see the block above
`jobs.migrate`), so a parser that degraded to a text scan would leave the other
tests green.

⚠ `docker-compose.yml` mounts `./infra` and `./.do` read-only into the backend
container, so these run there as well as on the CI runner. The skip below covers
only a partial checkout; it is hard-refused under `GITHUB_ACTIONS` so the fence
cannot silently disarm on the runner.
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest
import yaml


def _find_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    """Walk upward. `parents[2]` resolves to `/` inside the backend container,
    where this file lives at `/app/tests/`; walking up finds `/app`, which
    carries the `infra` and `.do` bind mounts."""
    for candidate in [start, *start.parents]:
        if (candidate / ".do" / "app.yaml").exists() and (
            candidate / "infra" / "MIGRATION.md"
        ).exists():
            return candidate
    return None


REPO_ROOT = _find_repo_root(pathlib.Path(__file__).resolve())

if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        ".do/app.yaml and infra/MIGRATION.md not found from a CI checkout; this "
        "fence must not be allowed to skip on the runner."
    )

pytestmark = pytest.mark.skipif(
    REPO_ROOT is None, reason="repo tree not present in this checkout"
)

APP_SPEC = (REPO_ROOT / ".do" / "app.yaml") if REPO_ROOT else None
RUNBOOK = (REPO_ROOT / "infra" / "MIGRATION.md") if REPO_ROOT else None

# The credentials the rotation procedure actually moves. Scoped deliberately:
# an unrelated new SECRET (an API key, say) is not a data-plane credential and
# must not trip this fence -- a fence that cries wolf on every new secret gets
# deleted.
DATA_PLANE_KEYS = frozenset({"DATABASE_URL", "REDIS_URL"})

# Every component that binds one. Kept as an explicit literal so that ADDING a
# binding is what goes red. Deriving this from the spec would compare the spec
# to itself and fence nothing.
EXPECTED_BINDINGS = frozenset(
    {
        ("backend", "DATABASE_URL"),
        ("backend", "REDIS_URL"),
        ("migrate", "DATABASE_URL"),
        ("migrate", "REDIS_URL"),
    }
)

_COMPONENT_KINDS = ("services", "jobs", "workers", "functions", "static_sites")

_COUNT_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four",
    5: "five", 6: "six", 7: "seven", 8: "eight",
}


def _spec_bindings(spec_text: str) -> frozenset[tuple[str, str]]:
    spec = yaml.safe_load(spec_text)
    found = set()
    for kind in _COMPONENT_KINDS:
        for comp in spec.get(kind) or []:
            for env in comp.get("envs") or []:
                if env.get("key") in DATA_PLANE_KEYS:
                    found.add((comp["name"], env["key"]))
    return frozenset(found)


def _section(md: str, head: str, tail: str) -> str:
    """Bound a section by its headers, failing informatively rather than raising
    a bare ValueError that reads like a broken test instead of a finding."""
    assert head in md, (
        f"infra/MIGRATION.md no longer contains {head!r}. This fence bounds a "
        "runbook section by that header -- update it here if it was renamed."
    )
    start = md.index(head)
    assert tail in md[start:], (
        f"infra/MIGRATION.md no longer contains {tail!r} after {head!r}, which "
        "this fence uses as the section's end boundary."
    )
    return md[start : md.index(tail, start)]


def _rotation_section(md: str) -> str:
    return _section(
        md,
        "## Credential rotation (TBD-414)",
        "### Why these are quotable in the first place",
    )


def _cutover_secrets_section(md: str) -> str:
    return _section(md, "### 6. Update App Platform secrets", "\n### 7.")


def _stated_counts(section: str) -> set[str]:
    """Count-words the prose states, tolerant of articles and casing."""
    hits = re.findall(
        r"(?:Re-encrypt|has)\s+(?:all\s+|the\s+)?(\w+)\s+(?:data-plane\s+)?"
        r"(?:secret\s+)?values",
        section,
        re.IGNORECASE,
    )
    return {h.lower() for h in hits}


# ─── the spec side ───────────────────────────────────────────────────────────


def test_app_spec_binds_exactly_the_expected_data_plane_credentials() -> None:
    """Kills: a fifth DATABASE_URL/REDIS_URL binding lands unnoticed, or
    `jobs.migrate.REDIS_URL` is deleted."""
    assert _spec_bindings(APP_SPEC.read_text()) == EXPECTED_BINDINGS


def test_the_spec_parser_ignores_a_comment_and_sees_a_real_binding() -> None:
    """Both directions, deliberately.

    A one-sided version is green for a parser that returns the expected set
    unconditionally, and `.do/app.yaml` names these keys in its own comments --
    so a parser that degraded to a text scan must be caught here.
    """
    spec = (
        "# rotate DATABASE_URL and REDIS_URL on the ghost component\n"
        "services:\n"
        "  - name: ghost\n"
        "    envs:\n"
        "      - key: NOT_A_CREDENTIAL\n"
        "        value: 'x'\n"
        "jobs:\n"
        "  - name: real\n"
        "    envs:\n"
        "      - key: REDIS_URL\n"
        "        type: SECRET\n"
        "        value: 'EV[...]'\n"
    )
    assert _spec_bindings(spec) == frozenset({("real", "REDIS_URL")})


def test_the_scan_reaches_every_component_the_spec_declares() -> None:
    """Anti-vacuity floor.

    A binding declared under a component kind absent from `_COMPONENT_KINDS` is
    invisible, and an invisible binding moves the count by zero -- so the strict
    equality above stays green while the runbook silently under-counts.
    """
    spec = yaml.safe_load(APP_SPEC.read_text())
    componentish = {
        k
        for k, v in spec.items()
        if isinstance(v, list)
        and any(isinstance(c, dict) and "envs" in c for c in v)
    }
    assert componentish, "no component in .do/app.yaml declares envs -- parse is broken"
    unscanned = componentish - set(_COMPONENT_KINDS)
    assert not unscanned, (
        f".do/app.yaml declares components under {sorted(unscanned)}, which "
        "_COMPONENT_KINDS does not scan; their credential bindings are invisible."
    )


# ─── the runbook side ────────────────────────────────────────────────────────


def test_rotation_runbook_enumerates_every_binding_the_spec_declares() -> None:
    """Naming the KEY alone would be satisfied by "re-encrypt DATABASE_URL";
    the operator needs to know it appears on TWO components."""
    section = _rotation_section(RUNBOOK.read_text())
    missing = [
        f"{component}.envs[{key}]"
        for component, key in sorted(EXPECTED_BINDINGS)
        if f"{component}.envs[{key}]" not in section
    ]
    assert not missing, (
        "the credential-rotation runbook does not name these bindings: "
        f"{missing}. Every data-plane binding in .do/app.yaml must be enumerated "
        "there, or the next rotation misses it silently."
    )


def test_cutover_runbook_enumerates_every_binding_the_spec_declares() -> None:
    """The site that was actually stale for three months.

    Step 6's table is a separate procedure from the rotation runbook and drifted
    independently; fencing only the rotation section would leave it rotting.
    """
    section = _cutover_secrets_section(RUNBOOK.read_text())
    missing = [
        f"{component}.envs[{key}]"
        for component, key in sorted(EXPECTED_BINDINGS)
        if f"{component}.envs[{key}]" not in section
    ]
    assert not missing, (
        f"the cutover's step 6 secrets table omits {missing}. It said THREE for "
        "three months after the spec grew a fourth binding."
    )


def test_both_runbooks_state_a_count_that_matches_the_spec() -> None:
    """Kills the literal 2026-08-20 defect: the shouted word going stale.

    The operator reads the count, not the bullet list.
    """
    n = len(EXPECTED_BINDINGS)
    assert n in _COUNT_WORDS, (
        f".do/app.yaml declares {n} data-plane bindings, which this fence has no "
        "word for. Extend _COUNT_WORDS and update both runbook sections."
    )
    md = RUNBOOK.read_text()
    for label, section in (
        ("credential-rotation step 4", _rotation_section(md)),
        ("cutover step 6", _cutover_secrets_section(md)),
    ):
        stated = _stated_counts(section)
        assert stated, (
            f"{label} no longer states how many values to update. It must say it "
            "in words -- the operator reads the count, not the list."
        )
        assert stated == {_COUNT_WORDS[n]}, (
            f"{label} states {sorted(stated)} but .do/app.yaml declares {n} "
            f"data-plane bindings ({_COUNT_WORDS[n]})."
        )
