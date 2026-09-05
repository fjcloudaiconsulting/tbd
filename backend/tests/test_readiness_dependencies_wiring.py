"""Structural fences for the ``/health/dependencies`` wiring (TBD-413).

A handler nothing can reach, and a check nothing runs, are both worthless. The
behavioural fences live in ``test_readiness_dependencies.py``; these assert the
plumbing around it.

⚠ Every assertion here PARSES the artifact. None is a whole-file grep. This
repo has three times shipped a "check" that a grep satisfied from the comment
documenting the very absence being checked for.

⚠ These fences read repo-root artifacts, which the backend container does not
lay out the way a checkout does: ``/app`` IS ``backend/``. They RAISE rather
than skip when an artifact cannot be found, following
``test_await_test_run_gate.py`` — a skip would make the fence silently absent
in whichever environment happened to lack the path, which is exactly how a
fence becomes decoration. ``docker-compose.yml`` carries the read-only mounts
that make all of them resolvable inside the container; a container built before
those mounts existed shows this module red until it is force-recreated, and
the error below says so.
"""
# TBD-495: S4 and S5 fenced the Helm chart's ingress rule and readiness
# probe. The chart was deleted as unused scaffolding -- it was named `pfv2`,
# was never deployed, and production is DO App Platform. Restore both fences
# alongside the chart if Kubernetes ever becomes a real target.
from __future__ import annotations

import os
import pathlib
import re

import yaml


def _strip_helm(text: str) -> str:
    """Drop Helm template lines so the chart parses as plain YAML.

    The charts are Helm templates: a bare ``yaml.safe_load`` dies on
    ``{{- if .Values... }}`` at line 1. Dropping every line containing ``{{``
    leaves the static structure these fences care about.
    """
    return "\n".join(line for line in text.splitlines() if "{{" not in line)


def _find_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "test.yml").exists():
            return candidate
    return None


REPO_ROOT = _find_repo_root(pathlib.Path(__file__).resolve())

# ⚠ The container mounts ``./.github`` read-only at ``/app/.github``, so the
# probe above SUCCEEDS there and returns ``/app``. An earlier cut of this
# module assumed the opposite and guarded itself with a ``skipif`` on
# ``REPO_ROOT is None``: that condition was unreachable, the skip never fired,
# and four fences hard-failed on FileNotFoundError in every container run. A
# false red is what gets a fence weakened rather than obeyed, so resolution is
# per-artifact and explicit below.
_CONTAINER_SCRIPTS = pathlib.Path("/app/repo-scripts")


def _artifact(relpath: str) -> pathlib.Path:
    """Locate a repo-root artifact in either layout.

    Raises rather than skipping, per ``test_await_test_run_gate.py``. The one
    path that genuinely differs is repo-root ``scripts/``: inside the container
    ``/app/scripts`` is already ``backend/scripts``, so the repo-root directory
    gets its own read-only mount at ``/app/repo-scripts``.
    """
    if REPO_ROOT is not None:
        candidate = REPO_ROOT / relpath
        if candidate.is_file():
            return candidate
    if relpath.startswith("scripts/"):
        alt = _CONTAINER_SCRIPTS / relpath[len("scripts/") :]
        if alt.is_file():
            return alt
    raise RuntimeError(
        f"Could not locate {relpath}. On a checkout it sits at the repo root; "
        "in the backend container it needs the read-only mount added for it in "
        "docker-compose.yml. A container built before that mount existed shows "
        "this module red — run `docker compose up -d --force-recreate backend` "
        "once, and do NOT weaken this fence."
    )


if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        "repo root not found from a CI checkout; these fences must not skip "
        "on the runner"
    )

ENDPOINT = "/health/dependencies"


def _migrations_job() -> dict:
    doc = yaml.safe_load(_artifact(".github/workflows/test.yml").read_text())
    return doc["jobs"]["migrations"]


def test_s1_ci_asserts_the_endpoint_by_parsing_json_not_grepping():
    """S1 — ``Migration Checks`` must actually assert the response.

    A bare status-code check is near-vacuous: a handler that always returns
    200 passes it. The step has to pin the values too, and it must parse the
    body rather than grep it.
    """
    job = _migrations_job()
    steps = [s for s in job["steps"] if ENDPOINT in (s.get("run") or "")]
    assert steps, (
        "no step in the `migrations` job exercises "
        f"{ENDPOINT}; the endpoint has no integration coverage"
    )
    run = "\n".join(s["run"] for s in steps)

    assert "json.load" in run, (
        "the assertion must PARSE the response body. A grep can be satisfied "
        "by a comment mentioning the key."
    )
    for pinned in ('d["status"]', '"ok"', '"disabled"', '"database"'):
        assert pinned in run, f"assertion does not pin {pinned}: {run}"


def test_s2_migration_checks_still_has_no_redis():
    """S2 — the CI branch under test is 'Redis genuinely absent'.

    If a future change adds ``REDIS_URL`` or a Redis service to this job, the
    step above silently stops testing the unconfigured branch and starts
    testing the connected one, while still passing. That is the whole reason
    the assertion pins ``redis == "disabled"``.
    """
    job = _migrations_job()
    assert "REDIS_URL" not in (job.get("env") or {}), (
        "Migration Checks now sets REDIS_URL, so it no longer exercises the "
        "unconfigured-Redis branch; update the assertion deliberately."
    )
    assert "redis" not in (job.get("services") or {}), (
        "Migration Checks now runs a Redis service; same problem."
    )


def test_s3_nginx_routes_the_endpoint_exactly():
    """S3 — dev nginx needs its own block.

    ``location = /health`` and ``location = /ready`` are EXACT matches, so
    neither covers the sub-path; without a block of its own the request falls
    through to Next.js and returns the SPA's 404 page.
    """
    conf = _artifact("nginx/default.conf").read_text()
    directives = [
        line.strip()
        for line in conf.splitlines()
        if line.strip().startswith("location ")
    ]
    assert f"location = {ENDPOINT} {{" in directives, (
        f"no exact-match nginx location for {ENDPOINT}; found {directives}"
    )


def test_s7_do_app_spec_routes_the_endpoint_to_the_backend():
    """S7 — the ONLY environment that is actually in production.

    S3 fences dev nginx, S1/S2 fence CI and S6 the post-deploy smoke test.
    Production is DO App Platform, driven by the committed ``.do/app.yaml``,
    and it had no fence at all — the one place where losing this route means
    the alarm silently stops existing.

    ⚠ The route is INCIDENTAL, which is exactly why it needs a fence. Unlike
    nginx's ``location =`` and the chart's ``pathType: Exact``, App Platform's
    rules are PREFIXES, so ``/health/dependencies`` reaches the backend only
    as a side effect of the ``prefix: /health`` rule that exists for
    ``/health``. Nothing in the file names this endpoint. Delete or narrow
    that rule and the request falls through to the ``prefix: /`` catch-all,
    which points at the FRONTEND — so a monitor gets Next.js's 200 HTML or its
    404 page instead of a dependency verdict, and the deploy gate in
    ``scripts/smoke-test.sh`` starts measuring the wrong component.

    Asserted under BOTH plausible matching semantics — longest-prefix wins,
    and first-rule-in-document-order wins — so the fence does not rest on a
    reading of App Platform's resolution order.
    """
    doc = yaml.safe_load(_artifact(".do/app.yaml").read_text())
    rules = doc["ingress"]["rules"]

    matching = [
        r
        for r in rules
        if "prefix" in (r.get("match") or {}).get("path", {})
        and ENDPOINT.startswith(r["match"]["path"]["prefix"])
    ]
    assert matching, (
        f"no ingress rule in .do/app.yaml matches {ENDPOINT} at all; rules "
        f"present: {[r.get('match') for r in rules]}"
    )

    longest = max(matching, key=lambda r: len(r["match"]["path"]["prefix"]))
    assert longest["component"]["name"] == "backend", (
        f"the most specific rule matching {ENDPOINT} is "
        f"{longest['match']['path']['prefix']!r} -> "
        f"{longest['component']['name']!r}. In production this endpoint must "
        "reach the backend; anything else serves the frontend's HTML to the "
        "uptime monitor and to scripts/smoke-test.sh."
    )
    assert matching[0]["component"]["name"] == "backend", (
        f"the first rule matching {ENDPOINT} in document order is "
        f"{matching[0]['match']['path']['prefix']!r} -> "
        f"{matching[0]['component']['name']!r}; more specific rules must stay "
        "above the catch-all."
    )


def test_s6_smoke_test_checks_the_endpoint_and_no_longer_lies_about_ready():
    """S6 — the post-deploy gate must cover Redis, and must stop claiming
    ``/ready`` already does.

    That comment was the only place the Redis check was documented on
    2026-08-19, and the script passed a deploy on which login was 100% broken.
    """
    script = _artifact("scripts/smoke-test.sh").read_text()

    checks = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("check_status ")
    ]
    assert any(ENDPOINT in c for c in checks), (
        f"smoke-test.sh does not check {ENDPOINT}; found {checks}"
    )

    # Target the header's numbered surface list precisely, rather than
    # sweeping every comment: prose elsewhere in the file legitimately
    # discusses the old false claim in order to explain why it was wrong,
    # and a crude "any comment mentioning both words" rule flags that too.
    surface = [
        line
        for line in script.splitlines()
        if re.match(r"^#\s+\d+\.\s+GET\s+/ready\b", line)
    ]
    assert surface, "could not find the /ready entry in smoke-test.sh's surface list"
    for line in surface:
        assert "redis" not in line.lower(), (
            "the surface list still claims /ready covers Redis. It does not: "
            f"it runs SELECT 1 and nothing else. {line!r}"
        )

    deps_surface = [
        line
        for line in script.splitlines()
        if re.match(r"^#\s+\d+\.\s+GET\s+" + re.escape(ENDPOINT), line)
    ]
    assert deps_surface, (
        f"{ENDPOINT} is checked but missing from the header surface list"
    )
    assert any("redis" in line.lower() for line in deps_surface), deps_surface
