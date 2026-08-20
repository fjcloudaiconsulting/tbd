"""TBD-425 -- ``scripts/ci/assert-app-spec-secrets-synced.sh`` refuses a deploy
that would overwrite a live App Platform secret with a stale committed one.

WHY THIS EXISTS

`digitalocean/app_action/deploy@v2` pushes the committed `.do/app.yaml` as the
AUTHORITATIVE spec, and every `type: SECRET` env var in that file carries an
encrypted `EV[...]` value. So a deploy silently replaces production's secrets
with whatever is in git.

`infra/MIGRATION.md` step 9 has warned about this since the 2026-05 cutover:
"If you skip this step, the next normal deploy reverts the live spec back to the
committed file, [...] pointing secrets at whatever was there before."

It was skipped. On 2026-08-20 a deploy pushed stale blobs and took production's
database and redis credentials down:

    (1045, "Access denied for user 'pfv_app'@'10.42.0.3' (using password: YES)")
    scheduler.tick.error: "invalid username-password pair or user is disabled."

A runbook comment did not prevent it, so the check is now executable.

⚠ EVERY assertion drives the REAL script with a stubbed ``doctl`` on PATH.
Nothing here re-implements the comparison; a test that restated the rules would
pass against a script that had them backwards.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


def _find_script() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        found = candidate / "scripts" / "ci" / "assert-app-spec-secrets-synced.sh"
        if found.is_file():
            return found
    container_mount = Path("/app/repo-scripts/ci/assert-app-spec-secrets-synced.sh")
    if container_mount.is_file():
        return container_mount
    raise RuntimeError(
        "Could not locate scripts/ci/assert-app-spec-secrets-synced.sh. In the "
        "backend container this needs the ./scripts:/app/repo-scripts:ro mount."
    )


SCRIPT = _find_script()


def _spec(omit: str | None = None, **overrides: str) -> str:
    """An app spec with enough SECRETs to clear the anti-vacuity floor.

    `omit` drops one env by key, so a "live has a secret the repo does not"
    case is built structurally rather than by string surgery.
    """
    vals = {
        "backend_db": "EV[1:aaa:AAA]",
        "backend_redis": "EV[1:bbb:BBB]",
        "backend_jwt": "EV[1:ccc:CCC]",
        "migrate_db": "EV[1:ddd:DDD]",
        "migrate_jwt": "EV[1:eee:EEE]",
        "extra": "EV[1:fff:FFF]",
    }
    vals.update(overrides)
    mfa = (
        ""
        if omit == "MFA_ENCRYPTION_KEY"
        else f"              - key: MFA_ENCRYPTION_KEY\n"
             f"                type: SECRET\n"
             f"                value: {vals['extra']}\n"
    )
    return textwrap.dedent(
        f"""\
        name: pfv
        services:
          - name: backend
            envs:
              - key: APP_ENV
                value: production
              - key: DATABASE_URL
                type: SECRET
                value: {vals['backend_db']}
              - key: REDIS_URL
                type: SECRET
                value: {vals['backend_redis']}
              - key: JWT_SECRET_KEY
                type: SECRET
                value: {vals['backend_jwt']}
{mfa}        jobs:
          - name: migrate
            kind: PRE_DEPLOY
            envs:
              - key: DATABASE_URL
                type: SECRET
                value: {vals['migrate_db']}
              - key: JWT_SECRET_KEY
                type: SECRET
                value: {vals['migrate_jwt']}
        """
    )


def _run(tmp_path: Path, committed: str, live: str | None, **env: str):
    """Drive the real script with a stubbed `doctl`."""
    spec_file = tmp_path / "app.yaml"
    spec_file.write_text(committed)

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if live is None:
        # doctl fails -> the live spec is unreadable.
        (bindir / "doctl").write_text("#!/usr/bin/env bash\nexit 1\n")
    else:
        live_file = tmp_path / "live.yaml"
        live_file.write_text(live)
        (bindir / "doctl").write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$2" = "list" ]; then echo "app-123 pfv"; exit 0; fi\n'
            f'cat "{live_file}"\n'
        )
    (bindir / "doctl").chmod(0o755)

    environ = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "SPEC_FILE": str(spec_file),
        "APP_ID": "app-123",
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=environ
    )


def test_identical_secrets_pass(tmp_path):
    r = _run(tmp_path, _spec(), _spec())
    assert r.returncode == 0, r.stderr
    assert "all 6 secrets match" in r.stdout


def test_a_drifted_secret_blocks_the_deploy(tmp_path):
    """The exact 2026-08-20 shape: the committed DATABASE_URL is stale."""
    r = _run(tmp_path, _spec(), _spec(backend_db="EV[1:zzz:ROTATED]"))
    assert r.returncode == 1
    assert "REFUSING TO DEPLOY" in r.stderr
    assert "backend/DATABASE_URL" in r.stderr
    assert "would OVERWRITE production's value" in r.stderr


def test_every_drifted_secret_is_named_not_just_the_first(tmp_path):
    """⚠ The incident changed THREE values. A guard that stops at the first
    sends the operator round the loop once per secret."""
    r = _run(
        tmp_path,
        _spec(),
        _spec(backend_db="EV[1:z:1]", backend_redis="EV[1:z:2]", migrate_db="EV[1:z:3]"),
    )
    assert r.returncode == 1
    for expected in ("backend/DATABASE_URL", "backend/REDIS_URL", "migrate/DATABASE_URL"):
        assert expected in r.stderr, f"{expected} not reported"


def test_a_live_only_secret_is_reported_as_a_deletion(tmp_path):
    committed = _spec(omit="MFA_ENCRYPTION_KEY")
    assert "MFA_ENCRYPTION_KEY" not in committed, "the omission did not apply"
    r = _run(tmp_path, committed, _spec())
    assert r.returncode == 1
    assert "would DELETE it" in r.stderr


def test_unreadable_live_spec_fails_closed(tmp_path):
    """⚠ Fail CLOSED. 'Assume it is fine' is what cost a production outage."""
    r = _run(tmp_path, _spec(), None)
    assert r.returncode == 1
    assert "cannot prove" in r.stderr


def test_break_glass_override_is_honoured_but_only_when_asked(tmp_path):
    """`deploy.yml` is documented as the ungated escape hatch, so the guard must
    be refusable there -- deliberately, never by default."""
    drifted = _spec(backend_db="EV[1:zzz:ROTATED]")
    blocked = _run(tmp_path, _spec(), drifted)
    assert blocked.returncode == 1

    allowed = _run(tmp_path, _spec(), drifted, ALLOW_SECRET_DRIFT="true")
    assert allowed.returncode == 0
    assert "SKIPPED" in allowed.stdout

    # Anything other than the exact string must NOT disable the guard.
    for sneaky in ("True", "1", "yes", ""):
        r = _run(tmp_path, _spec(), drifted, ALLOW_SECRET_DRIFT=sneaky)
        assert r.returncode == 1, f"ALLOW_SECRET_DRIFT={sneaky!r} disabled the guard"


def test_a_spec_that_parses_to_almost_nothing_does_not_pass_vacuously(tmp_path):
    """⚠ Anti-vacuity floor. If the spec shape changes and the parse yields two
    secrets instead of fifteen, 'they all match' is meaningless."""
    tiny = textwrap.dedent(
        """\
        name: pfv
        services:
          - name: backend
            envs:
              - key: DATABASE_URL
                type: SECRET
                value: EV[1:a:A]
        """
    )
    r = _run(tmp_path, tiny, tiny)
    assert r.returncode == 1
    assert "Refusing to certify" in r.stderr
