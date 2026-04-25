import os
import subprocess
from pathlib import Path

import pytest


ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"


@pytest.fixture
def fake_alembic_env(tmp_path):
    """PATH-shimmed env with a stub `alembic` binary that records its argv."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "log.txt"

    def _make(stub_body: str):
        alembic = fake_bin / "alembic"
        alembic.write_text(stub_body)
        alembic.chmod(0o755)
        return {
            "env": {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
            "log": log,
        }

    return _make


def test_entrypoint_runs_alembic_before_cmd(fake_alembic_env, tmp_path):
    """The image-level migration contract: alembic upgrade head ALWAYS runs
    before the CMD, on every container start, regardless of env. Locks down
    the fix for the 2026-04-24 SSO outage where migration 025 silently never
    ran on prod because the App Platform spec lacked a PRE_DEPLOY job."""
    ctx = fake_alembic_env(
        f'#!/bin/sh\necho "alembic $*" >> {tmp_path / "log.txt"}\n'
    )
    cmd_marker = f'echo cmd >> {tmp_path / "log.txt"}'

    result = subprocess.run(
        [str(ENTRYPOINT), "/bin/sh", "-c", cmd_marker],
        env=ctx["env"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    log_lines = ctx["log"].read_text().strip().splitlines()
    assert log_lines == ["alembic upgrade head", "cmd"]


def test_entrypoint_aborts_when_migration_fails(fake_alembic_env, tmp_path):
    """If alembic exits non-zero the entrypoint must NOT exec the CMD —
    we want the container to die and the orchestrator to keep the previous
    healthy version, not roll out a process pointed at a half-migrated DB."""
    ctx = fake_alembic_env("#!/bin/sh\nexit 1\n")
    cmd_marker = f'echo cmd-should-not-run >> {tmp_path / "log.txt"}'

    result = subprocess.run(
        [str(ENTRYPOINT), "/bin/sh", "-c", cmd_marker],
        env=ctx["env"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    log = ctx["log"]
    assert not log.exists() or "cmd-should-not-run" not in log.read_text()
