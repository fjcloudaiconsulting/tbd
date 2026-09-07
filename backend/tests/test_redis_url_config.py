"""REDIS_URL is required in production (TBD-438).

Redis is the auth **session store**. Every token-issue path in
``routers/auth.py`` fails closed without it, so a production instance booted
with ``REDIS_URL`` unset cannot log anybody in — it comes up healthy-looking
and refuses every login.

Why a boot refusal is earned here
---------------------------------
``config.py`` already states the criterion, in the note explaining why
``founder_count_exclude_usernames`` deliberately does NOT get one:

    "the blast radius of a boot refusal is the whole application down.
    ``api_token_hmac_key`` earns its prod-required validator because losing it
    breaks PAT authentication -- a security primitive."

Redis is the session store for interactive auth, which is the same class of
primitive. This validator applies the existing rule rather than inventing a
new one, and mirrors ``_validate_api_token_hmac_key`` in shape.

⚠ THE COUPLED CHANGE THIS FILE EXISTS TO PROTECT.
``backend/scripts/migrate.py`` imports ``app.logging`` (line 42), which does
``from app.config import settings`` (``app/logging.py:7``), which constructs
``Settings()`` at module import (``config.py:601``). The App Platform
PRE_DEPLOY migrate job runs with ``APP_ENV=production``. So the moment this
validator exists, **the migrate job needs REDIS_URL bound or every production
deploy fails before any backend replica starts.**

``.do/app.yaml`` binds it today, but carried a comment saying the job "does
NOT require this" and that the value was "synced rather than dropped" — an
explicit invitation to delete it. That comment is rewritten in the same commit
as this validator. ``test_migrate_job_binds_redis_url`` below is the fence on
the binding itself, so deleting it fails here rather than in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import Settings

_VALID_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_VALID_PAT_KEY = "dedicated-pat-hmac-key-distinct-and-32plus-chars"
_REDIS = "redis://10.42.0.5:6379/0"


def _settings(**overrides) -> Settings:
    """Construct Settings off the real defaults, never the developer's .env.

    ``_env_file=None`` matters: without it a populated local ``.env`` supplies
    ``redis_url`` and the required-in-production test passes vacuously on the
    author's machine while failing in CI (or vice versa).
    """
    base = {"_env_file": None, "jwt_secret_key": _VALID_JWT}
    base.update(overrides)
    return Settings(**base)


def _prod(**overrides) -> Settings:
    """A production Settings with every OTHER prod-required knob satisfied.

    ``api_token_hmac_key`` is required in production too (``config.py:508``).
    Supplying it here is what makes a raised ValidationError unambiguously
    about REDIS_URL — otherwise this file would go green on the wrong error.
    """
    base = {"app_env": "production", "api_token_hmac_key": _VALID_PAT_KEY}
    base.update(overrides)
    return _settings(**base)


# ── The ruling ──────────────────────────────────────────────────────────────


def test_production_refuses_to_construct_without_redis_url():
    """F1: the fence. Kills 'no validator at all'."""
    with pytest.raises(ValidationError, match="REDIS_URL"):
        _prod(redis_url="")


def test_production_accepts_a_configured_redis_url():
    """F2: positive control. Kills a validator that raises unconditionally.

    Without this, ``raise ValueError("REDIS_URL is required in production")``
    with no guard at all would satisfy the test above and break every deploy.
    """
    assert _prod(redis_url=_REDIS).redis_url == _REDIS


def test_whitespace_only_redis_url_is_refused_in_production():
    """F3: kills a truthiness check that a space satisfies.

    ``redis_url = " "`` is truthy, so a bare ``if not self.redis_url`` passes
    it through and the app boots with an unusable connection string.
    """
    with pytest.raises(ValidationError, match="REDIS_URL"):
        _prod(redis_url="   ")


@pytest.mark.parametrize("env", ["development", "test", "staging"])
def test_non_production_still_constructs_without_redis_url(env):
    """F4: kills a validator that forgot to scope itself to production.

    Local development runs without Redis by design. A validator missing its
    ``app_env`` branch would break every developer's stack and the whole test
    suite, so this is also the blast-radius control on the change itself.
    """
    assert _settings(app_env=env, redis_url="").redis_url == ""


def test_the_error_names_the_variable_an_operator_must_set():
    """The message is the whole value of a boot refusal.

    A deploy that dies at PRE_DEPLOY gives the operator one string to act on.
    ``REDIS_URL`` must appear literally, in the env-var spelling, not as
    ``redis_url``.
    """
    with pytest.raises(ValidationError) as exc:
        _prod(redis_url="")
    assert "REDIS_URL" in str(exc.value)


# ── The coupled change: the PRE_DEPLOY binding this validator now requires ──


def _app_spec() -> dict:
    """Read the committed App Platform spec.

    Mounted read-only into the backend container for exactly this class of
    fence; ``test_deploy_workflow.py`` reads it the same way.
    """
    path = Path("/app/.do/app.yaml")
    if not path.exists():  # plain checkout in CI
        path = Path(__file__).resolve().parents[2] / ".do" / "app.yaml"
    return yaml.safe_load(path.read_text())


def test_migrate_job_binds_redis_url():
    """⚠ THE LOAD-BEARING FENCE. Kills the deletion this change invites.

    ``migrate.py`` -> ``app.logging`` -> ``app.config`` -> ``Settings()`` at
    import, under ``APP_ENV=production``. Drop this binding and PRE_DEPLOY
    raises before a single migration runs, so **no production deploy
    completes** — and the failure is in a job most people never look at.

    Asserting the binding EXISTS is the point: the ``.do/app.yaml`` comment
    used to invite deleting it.
    """
    jobs = _app_spec()["jobs"]
    migrate = next(j for j in jobs if j["name"] == "migrate")
    keys = {e["key"] for e in migrate["envs"]}
    assert "REDIS_URL" in keys, (
        "The PRE_DEPLOY migrate job must bind REDIS_URL. migrate.py imports "
        "app.logging -> app.config, which constructs Settings() at import "
        "under APP_ENV=production, where redis_url is now required. Removing "
        "this binding breaks every production deploy at the migrate step."
    )


def test_migrate_job_runs_as_production():
    """Pins the premise of the fence above.

    If APP_ENV ever stopped being "production" here, the binding would no
    longer be load-bearing and ``test_migrate_job_binds_redis_url`` would
    still pass while guarding nothing. This makes that drift visible.
    """
    jobs = _app_spec()["jobs"]
    migrate = next(j for j in jobs if j["name"] == "migrate")
    app_env = next(e for e in migrate["envs"] if e["key"] == "APP_ENV")
    assert app_env["value"] == "production"


def test_app_yaml_comment_does_not_invite_dropping_the_binding():
    """The comment is the actual hazard, so fence the comment.

    The original text said the migrate job "does NOT require this" and that
    the value was "synced rather than dropped". Both were true before the
    validator and are false after it. A future reader acting on that text
    breaks production, and no schema-level assertion would catch it because
    the spec would still be valid YAML.
    """
    path = Path("/app/.do/app.yaml")
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / ".do" / "app.yaml"
    text = path.read_text()
    assert "does NOT require this" not in text, (
        "The PRE_DEPLOY REDIS_URL comment still tells the reader the job does "
        "not need the binding. Since TBD-438 it does — Settings refuses to "
        "construct in production without it."
    )
