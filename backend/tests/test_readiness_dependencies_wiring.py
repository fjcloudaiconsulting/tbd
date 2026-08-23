"""Structural fences for the ``/health/dependencies`` wiring (TBD-413).

A handler nothing can reach, and a check nothing runs, are both worthless. The
behavioural fences live in ``test_readiness_dependencies.py``; these assert the
plumbing around it.

⚠ Every assertion here PARSES the artifact. None is a whole-file grep. This
repo has three times shipped a "check" that a grep satisfied from the comment
documenting the very absence being checked for.

⚠ ``.github/``, ``infra/``, ``nginx/`` and ``k8s/`` are NOT mounted into the
backend dev container (``docker-compose.yml`` mounts only ``backend/app``,
``backend/alembic``, ``backend/scripts``, ``backend/tests`` plus a few single
files). These therefore SKIP locally and run in CI, which is the same shape
``test_deploy_drift_probe.py`` uses. The CI guard below makes a skip on the
runner an error rather than a silent pass.
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest
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

if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        "repo root not found from a CI checkout; these fences must not skip "
        "on the runner"
    )

pytestmark = pytest.mark.skipif(
    REPO_ROOT is None,
    reason="repo tree is not mounted into the backend container; runs in CI",
)

ENDPOINT = "/health/dependencies"


def _migrations_job() -> dict:
    doc = yaml.safe_load((REPO_ROOT / ".github/workflows/test.yml").read_text())
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
    conf = (REPO_ROOT / "nginx/default.conf").read_text()
    directives = [
        line.strip()
        for line in conf.splitlines()
        if line.strip().startswith("location ")
    ]
    assert f"location = {ENDPOINT} {{" in directives, (
        f"no exact-match nginx location for {ENDPOINT}; found {directives}"
    )


def test_s4_k8s_ingress_routes_the_endpoint_exactly():
    """S4 — the k8s chart's ``/health`` rule is ``pathType: Exact``."""
    doc = yaml.safe_load(
        _strip_helm((REPO_ROOT / "k8s/templates/ingress.yaml").read_text())
    )

    # Collect path entries by walking the tree rather than navigating a fixed
    # shape. Stripping Helm lines removes `- host: {{ ... }}`, which collapses
    # `spec.rules` from a list into a mapping — so `doc["spec"]["rules"][0]`
    # is not a stable seam. The `service.name` is templated away entirely,
    # which is why this asserts routing shape and not the backend name.
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if "path" in node and "pathType" in node:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    assert found, "parsed no ingress path entries at all"

    match = [e for e in found if e["path"] == ENDPOINT]
    assert match, (
        f"no ingress rule for {ENDPOINT}; /health is pathType Exact so it "
        f"does not cover it. Paths present: {[e['path'] for e in found]}"
    )
    assert match[0]["pathType"] == "Exact", match[0]


def test_s5_k8s_readiness_probe_stays_on_ready_and_is_bounded():
    """S5 — two things at once, both regressions someone would plausibly ship.

    The probe must keep pointing at ``/ready`` (repointing it at the
    dependency endpoint would evict every replica on a shared-Redis outage),
    and it must carry an explicit ``timeoutSeconds`` because the k8s DEFAULT
    IS 1s — under the app's own 3.0s database bound.
    """
    safe = _strip_helm((REPO_ROOT / "k8s/templates/backend.yaml").read_text())
    # The file holds several documents (Deployment, Service, ...).
    docs = [d for d in yaml.safe_load_all(safe) if isinstance(d, dict)]
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    assert deployments, f"no Deployment in the chart; kinds: {[d.get('kind') for d in docs]}"
    container = deployments[0]["spec"]["template"]["spec"]["containers"][0]
    probe = container["readinessProbe"]

    assert probe["httpGet"]["path"] == "/ready", (
        "the readinessProbe must stay on /ready. Pointing it at "
        f"{ENDPOINT} makes a Redis outage evict every replica at once."
    )
    assert probe.get("timeoutSeconds", 1) >= 3, (
        "readinessProbe needs an explicit timeoutSeconds >= 3; the k8s "
        "default is 1s, which is under the app's own database probe bound."
    )


def test_s6_smoke_test_checks_the_endpoint_and_no_longer_lies_about_ready():
    """S6 — the post-deploy gate must cover Redis, and must stop claiming
    ``/ready`` already does.

    That comment was the only place the Redis check was documented on
    2026-08-19, and the script passed a deploy on which login was 100% broken.
    """
    script = (REPO_ROOT / "scripts/smoke-test.sh").read_text()

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
