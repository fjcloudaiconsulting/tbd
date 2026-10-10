"""Fences: no credential reaches curl's argv in a repo script (INFRA-95).

argv is world-readable (`ps`, /proc/<pid>/cmdline) and is what a shell trace
or a crash dump prints. smoke-test.sh used to pass the smoke password in
`--data` and the bearer token in `-H`; update-coverage-badge.sh passed
GIST_TOKEN in `-H`. Both now feed them to curl on stdin.

Each test runs the real script with a stub `curl` first on PATH. The stub
records its argv and stdin and answers like a healthy server, so a test
asserts three things at once: no secret in curl's argv, the secret still
reaches curl (on stdin, in the shape curl expects), and the script's exit code
is unchanged. The stub only watches curl: a secret moved into another external
command's argv (`env printf ...`) would pass, so keep feeding curl from the
printf builtin. If the stub were bypassed the log would be empty and every test
fails on that first; the URLs are `smoke.invalid` and a fake gist id.
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


def _script(relpath: str) -> Path:
    """Repo-root `scripts/` on a checkout, /app/repo-scripts in the dev
    container (docker-compose.yml). Raises rather than skips: a skipped fence
    is an absent one."""
    root = _find_repo_root(Path(__file__).resolve())
    for c in ([root / "scripts" / relpath] if root else []) + [Path("/app/repo-scripts") / relpath]:
        if c.is_file():
            return c
    raise FileNotFoundError(f"scripts/{relpath} not found (needs ./scripts:/app/repo-scripts:ro)")


PASSWORD = "pw-INFRA95-do-not-leak"
USERNAME = "smoke-INFRA95@example.test"
TOKEN = "tok-INFRA95-access-token-do-not-leak"
GIST_TOKEN = "ghp-INFRA95-gist-token-do-not-leak"

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read() if "@-" in args else ""
with open(os.environ["CURL_STUB_LOG"], "a") as f:
    f.write(json.dumps({"argv": args, "stdin": stdin}) + "\n")
def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None
url = next(a for a in args if a.startswith("http"))
body = "{}"
if url.endswith("/api/v1/auth/login"):
    body = json.dumps({"access_token": os.environ["CURL_STUB_TOKEN"]})
    with open(opt("-D"), "w") as h:
        h.write("HTTP/1.1 200 OK\r\nset-cookie: refresh_token=r; HttpOnly\r\n\r\n")
if opt("-o"):
    with open(opt("-o"), "w") as o:
        o.write(body)
sys.stdout.write("200")
'''


def _run(tmp_path: Path, script: Path, args: list[str], env: dict) -> tuple[subprocess.CompletedProcess, list[dict]]:
    stub = tmp_path / "bin" / "curl"
    stub.parent.mkdir()
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "curl.log"
    r = subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, **env, "PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}",
             "CURL_STUB_LOG": str(log), "CURL_STUB_TOKEN": TOKEN},
    )
    calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
    assert calls, f"stub curl never ran (real curl on PATH?)\n{r.stdout}\n{r.stderr}"
    return r, calls


def _assert_not_in_argv(calls: list[dict], *secrets: str) -> None:
    for call in calls:
        for secret in secrets:
            assert not any(secret in a for a in call["argv"]), f"secret in curl argv: {call['argv']}"


def test_smoke_test_keeps_password_and_token_out_of_argv(tmp_path: Path) -> None:
    r, calls = _run(tmp_path, _script("smoke-test.sh"), [], {
        "SMOKE_BASE_URL": "https://smoke.invalid/",
        "SMOKE_USERNAME": USERNAME, "SMOKE_PASSWORD": PASSWORD,
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All smoke checks passed." in r.stdout
    _assert_not_in_argv(calls, PASSWORD, TOKEN)
    for secret in (PASSWORD, TOKEN):
        assert secret not in r.stdout + r.stderr

    by_url = {next(a for a in c["argv"] if a.startswith("http")): c for c in calls}
    login = by_url["https://smoke.invalid/api/v1/auth/login"]
    assert json.loads(login["stdin"]) == {"login": USERNAME, "password": PASSWORD}
    assert login["argv"][login["argv"].index("Content-Type: application/json") - 1] == "-H"
    read = by_url["https://smoke.invalid/api/v1/categories"]
    assert read["stdin"].rstrip("\n") == f"Authorization: Bearer {TOKEN}"
    assert read["argv"][read["argv"].index("@-") - 1] == "-H"


@pytest.mark.parametrize("env,code", [({"SMOKE_BASE_URL": ""}, 2), ({"SMOKE_PASSWORD": ""}, 2)])
def test_smoke_test_exit_codes_unchanged(tmp_path: Path, env: dict, code: int) -> None:
    full = {"SMOKE_BASE_URL": "https://smoke.invalid", "SMOKE_USERNAME": USERNAME,
            "SMOKE_PASSWORD": PASSWORD, **env}
    r = subprocess.run(["bash", str(_script("smoke-test.sh"))], capture_output=True,
                       text=True, timeout=180, env={**os.environ, **full})
    assert r.returncode == code, r.stdout + r.stderr


def test_coverage_badge_keeps_gist_token_out_of_argv(tmp_path: Path) -> None:
    r, calls = _run(tmp_path, _script("ci/update-coverage-badge.sh"), ["backend", "91.5"], {
        "GIST_TOKEN": GIST_TOKEN, "GIST_ID": "infra95-not-a-gist",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert "update-coverage-badge: backend = 91.5%" in r.stdout, r.stdout + r.stderr
    _assert_not_in_argv(calls, GIST_TOKEN)
    (call,) = calls
    assert call["stdin"].rstrip("\n") == f"Authorization: Bearer {GIST_TOKEN}"
    assert call["argv"][call["argv"].index("@-") - 1] == "-H"
