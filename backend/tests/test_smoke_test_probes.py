"""INF-09: the post-deploy smoke reads only what the public hosts route to the backend.

The public hosts send ``/api`` and exactly ``/health`` to the backend; ``/ready``
and ``/health/dependencies`` query the database and are read inside the cluster
(kubelet probes ``/ready`` on the pod port). cloud-infra's post-deploy smoke
fetches ``scripts/smoke-test.sh`` at the deployed tag and runs it against the
public URL, so a script that still required those two routes would fail every
deploy once they stop being routed (they answer the frontend's 404). They stay
checkable behind ``SMOKE_INTERNAL_PROBES=1``, for a base URL that reaches the
backend directly.

Each test runs the real script with a stub ``curl`` first on PATH that records
the URLs it is asked for and answers with a status per path (200 by default).
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


def _find_repo_root(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    return None


def _script() -> Path:
    """Repo-root `scripts/` on a checkout, /app/repo-scripts in the dev
    container (docker-compose.yml). Raises rather than skips."""
    root = _find_repo_root(Path(__file__).resolve())
    for c in ([root / "scripts" / "smoke-test.sh"] if root else []) + [Path("/app/repo-scripts/smoke-test.sh")]:
        if c.is_file():
            return c
    raise FileNotFoundError("scripts/smoke-test.sh not found (needs ./scripts:/app/repo-scripts:ro)")


BASE = "https://smoke.invalid"
HEALTH, READY, DEPS = "/health", "/ready", "/health/dependencies"
LOGIN, READ = "/api/v1/auth/login", "/api/v1/categories"

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if "@-" in args:
    sys.stdin.read()
def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None
url = next(a for a in args if a.startswith("http"))
path = url[len(os.environ["CURL_STUB_BASE"]):]
with open(os.environ["CURL_STUB_LOG"], "a") as f:
    f.write(path + "\n")
status = json.loads(os.environ["CURL_STUB_STATUS"]).get(path, 200)
body = "{}"
if path == "/api/v1/auth/login" and status == 200:
    body = json.dumps({"access_token": "tok-INF09-0123456789abcdef"})
    with open(opt("-D"), "w") as h:
        h.write("HTTP/1.1 200 OK\r\nset-cookie: refresh_token=r; HttpOnly\r\n\r\n")
if opt("-o"):
    with open(opt("-o"), "w") as o:
        o.write(body)
sys.stdout.write(str(status))
'''


def _run(tmp_path: Path, status: dict[str, int] | None = None, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    stub = tmp_path / "bin" / "curl"
    stub.parent.mkdir()
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "curl.log"
    base_env = {k: v for k, v in os.environ.items() if k != "SMOKE_INTERNAL_PROBES"}
    r = subprocess.run(
        ["bash", str(_script())],
        capture_output=True, text=True, timeout=180,
        env={**base_env, "SMOKE_BASE_URL": BASE + "/", "SMOKE_USERNAME": "smoke@example.test",
             "SMOKE_PASSWORD": "pw-INF09", **env,
             "PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}",
             "CURL_STUB_LOG": str(log), "CURL_STUB_BASE": BASE,
             "CURL_STUB_STATUS": json.dumps(status or {})},
    )
    paths = log.read_text().splitlines() if log.exists() else []
    return r, paths


def test_default_run_skips_the_internal_probes_and_passes_when_they_are_not_routed(tmp_path: Path) -> None:
    """The public host answers the frontend's 404 for both probes: the smoke still passes."""
    r, paths = _run(tmp_path, {READY: 404, DEPS: 404})

    assert r.returncode == 0, r.stdout + r.stderr
    assert "All smoke checks passed." in r.stdout
    assert paths == [HEALTH, LOGIN, READ], paths
    assert "SMOKE_INTERNAL_PROBES=1" in r.stdout, "the skip must say how to turn the probes on"


@pytest.mark.parametrize("path", [HEALTH, LOGIN, READ])
def test_default_run_still_fails_on_the_public_checks(tmp_path: Path, path: str) -> None:
    r, paths = _run(tmp_path, {path: 503})

    assert r.returncode == 1, r.stdout + r.stderr
    assert "Smoke checks FAILED." in r.stdout
    assert path in paths


def test_opt_in_checks_both_internal_probes(tmp_path: Path) -> None:
    r, paths = _run(tmp_path, SMOKE_INTERNAL_PROBES="1")

    assert r.returncode == 0, r.stdout + r.stderr
    assert paths == [HEALTH, READY, DEPS, LOGIN, READ], paths


@pytest.mark.parametrize("path", [READY, DEPS])
def test_opt_in_fails_when_an_internal_probe_fails(tmp_path: Path, path: str) -> None:
    r, _ = _run(tmp_path, {path: 503}, SMOKE_INTERNAL_PROBES="1")

    assert r.returncode == 1, r.stdout + r.stderr
    assert f"GET {path}: expected 200, got 503" in r.stdout


@pytest.mark.parametrize("value", ["0", ""])
def test_explicit_off_is_the_default(tmp_path: Path, value: str) -> None:
    r, paths = _run(tmp_path, {READY: 404, DEPS: 404}, SMOKE_INTERNAL_PROBES=value)

    assert r.returncode == 0, r.stdout + r.stderr
    assert paths == [HEALTH, LOGIN, READ], paths


@pytest.mark.parametrize("value", ["true", "yes", "2"])
def test_any_other_value_is_refused_before_a_request(tmp_path: Path, value: str) -> None:
    """A typo must not silently drop the checks the operator asked for."""
    r, paths = _run(tmp_path, SMOKE_INTERNAL_PROBES=value)

    assert r.returncode == 2, r.stdout + r.stderr
    assert "SMOKE_INTERNAL_PROBES" in r.stdout
    assert paths == []
