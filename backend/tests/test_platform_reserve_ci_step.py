"""TBD-586 R12/R16/R18: the MySQL-gated reserve fences must run in CI, last,
and a skip there must be red."""
from pathlib import Path

import yaml


def _root() -> Path:
    for c in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (c / ".github" / "workflows" / "test.yml").exists():
            return c
    raise RuntimeError("repo root not found")


def _job_steps():
    wf = yaml.safe_load((_root() / ".github/workflows/test.yml").read_text())
    return wf["jobs"]["migrations"]["steps"]


def test_the_reserve_fences_are_the_last_step_of_the_migrations_job():
    last = _job_steps()[-1]
    assert "test_platform_reserve_mysql.py" in last["run"]
    assert last["env"]["PLATFORM_RESERVE_MYSQL_URL"] == "${{ env.DATABASE_URL }}"


def test_a_missing_url_or_a_skip_is_red_in_ci():
    last = _job_steps()[-1]
    assert last["env"]["PLATFORM_RESERVE_MYSQL_REQUIRED"] == "1"
    assert "pipefail" in last["run"] and "^SKIPPED" in last["run"] and "exit 1" in last["run"]
    src = (Path(__file__).parent / "services/test_platform_reserve_mysql.py").read_text()
    assert "PLATFORM_RESERVE_MYSQL_REQUIRED" in src and "raise RuntimeError" in src
