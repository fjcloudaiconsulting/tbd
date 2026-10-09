"""Sessions, single-use tokens and leases move to MySQL (INFRA-122): the
state_db contract on SQLite. The races run on real MySQL in
tests/test_state_db_mysql.py."""
from __future__ import annotations

from app import state_db as s
from tests.conftest import expire_family, expire_grace, expire_lease, expire_token, state_family, state_jtis

TTL = 3600


def _new(sid="sid1", jti="A", user=7):
    s._issue(jti, sid, user, TTL)
    return sid


def test_g1_issue_then_reads():
    _new()
    assert s._validate("A") == {"user_id": 7, "sid": "sid1"}
    assert s._family_exists("sid1") and s._family_member("sid1", "A")
    assert not s._family_member("sid1", "garbage")
    assert s._grace("A") is None
    assert s._validate("garbage") is None


def test_f1_rotate_moves_head_and_graces_old():
    _new()
    assert s._rotate("A", "B", "sid1", 7, TTL) == s.SESSION_ROTATE_OK
    assert s._validate("A") is None
    assert s._validate("B") == {"user_id": 7, "sid": "sid1"}
    assert s._grace("A") == {"user_id": 7, "sid": "sid1", "successor_jti": "B"}
    assert s._grace("B") is None
    assert state_jtis("sid1") == {"A", "B"}


def test_f1b_rotate_guards():
    _new()
    assert s._rotate("A", "B", "sid1", 7, TTL) == s.SESSION_ROTATE_OK
    assert s._rotate("A", "C", "sid1", 7, TTL) == s.SESSION_ROTATE_ALREADY_ROTATED
    assert s._rotate("X", "C", "sid1", 7, TTL) == s.SESSION_ROTATE_REVOKED
    assert s._rotate("B", "C", "sid1", 8, TTL) == s.SESSION_ROTATE_REVOKED
    assert s._rotate("B", "A", "sid1", 7, TTL) == s.SESSION_ROTATE_JTI_COLLISION
    assert state_family("sid1")["head_jti"] == "B"
    assert state_jtis("sid1") == {"A", "B"}


def test_f2_burst_keeps_first_jti_graced_with_live_successor():
    _new()
    s._rotate("A", "B", "sid1", 7, TTL)
    s._rotate("B", "C", "sid1", 7, TTL)
    assert s._grace("A") == {"user_id": 7, "sid": "sid1", "successor_jti": "C"}
    assert s._detect_reuse_and_revoke("A", "sid1") == (s.SESSION_REUSE_GRACE,)


def test_f3_grace_does_not_slide_with_later_rotations():
    """A->B, 31 s later B->C: A's own window closed, so A is reuse even though
    the family rotated a moment ago."""
    _new()
    s._rotate("A", "B", "sid1", 7, TTL)
    expire_grace("sid1")
    s._rotate("B", "C", "sid1", 7, TTL)
    assert s._grace("B") is not None
    assert s._grace("A") is None
    assert s._detect_reuse_and_revoke("A", "sid1") == (s.SESSION_REUSE_REUSED, 3)
    assert state_family("sid1") is None and state_jtis("sid1") == set()


def test_f4_detect_classes():
    _new()
    assert s._detect_reuse_and_revoke("A", "sid1") == (s.SESSION_REUSE_LIVE,)
    s._rotate("A", "B", "sid1", 7, TTL)
    assert s._detect_reuse_and_revoke("A", "sid1") == (s.SESSION_REUSE_GRACE,)
    assert s._detect_reuse_and_revoke("garbage", "sid1") == (s.SESSION_REUSE_UNKNOWN,)
    expire_grace("sid1")
    assert s._detect_reuse_and_revoke("A", "sid1") == (s.SESSION_REUSE_REUSED, 2)
    assert s._detect_reuse_and_revoke("A", "sid1") == (s.SESSION_REUSE_UNKNOWN,)


def test_f5_revoke_family():
    _new()
    s._rotate("A", "B", "sid1", 7, TTL)
    assert s._revoke_family("sid1") == ["A", "B"]
    assert s._revoke_family("sid1") == []
    assert s._rotate("B", "C", "sid1", 7, TTL) == s.SESSION_ROTATE_REVOKED
    assert s._validate("B") is None and not s._family_exists("sid1")


def _age(jti, seconds):
    from sqlalchemy import update

    with s._engine.begin() as c:
        c.execute(update(s._M).where(s._M.c.jti == jti).values(created_at=s.db_now(-seconds)))


def test_f3b_rotations_under_30s_apart_do_not_extend_older_grace():
    """A->B 35 s ago, B->C 20 s ago, C->D now: only B and C are graced."""
    _new()
    s._rotate("A", "B", "sid1", 7, TTL)
    _age("B", 35)
    s._rotate("B", "C", "sid1", 7, TTL)
    _age("C", 20)
    s._rotate("C", "D", "sid1", 7, TTL)
    assert s._grace("A") is None
    assert s._grace("B") == {"user_id": 7, "sid": "sid1", "successor_jti": "D"}
    assert s._grace("C") is not None
    assert s._detect_reuse_and_revoke("B", "sid1") == (s.SESSION_REUSE_GRACE,)
    assert s._detect_reuse_and_revoke("A", "sid1") == (s.SESSION_REUSE_REUSED, 4)


def test_g4_seq_clash_is_not_a_jti_collision():
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    _new()
    s._rotate("A", "B", "sid1", 7, TTL)
    with s._engine.begin() as c:  # a counter out of step with the members
        c.execute(update(s._F).where(s._F.c.sid == "sid1").values(rotations=0))
    import pytest

    with pytest.raises(IntegrityError):
        s._rotate("B", "C", "sid1", 7, TTL)


def test_g2_expired_family_is_dead():
    _new()
    expire_family("sid1")
    assert s._validate("A") is None
    assert not s._family_exists("sid1")
    assert s._rotate("A", "B", "sid1", 7, TTL) == s.SESSION_ROTATE_REVOKED
    assert s._detect_reuse_and_revoke("A", "sid1") == (s.SESSION_REUSE_UNKNOWN,)


def test_g3_jtis_differ_by_case():
    _new(jti="AbC")
    assert s._validate("abc") is None
    assert s._validate("AbC") is not None


def test_f6_claim_token():
    assert s._claim("mfa_email", "j1", 60) is True
    assert s._claim("mfa_email", "j1", 60) is False
    assert s._claim("mailgun_webhook", "j1", 60) is True
    expire_token("mfa_email", "j1")
    assert s._claim("mfa_email", "j1", 60) is True
    assert s._claim("mfa_email", "j1", 60) is False


def test_f7_lease():
    h = s._acquire_lease("agent:turn:1", 180)
    assert h and len(h) == 32
    assert s._acquire_lease("agent:turn:1", 180) is None
    assert s._acquire_lease("agent:turn:2", 180) is not None
    s._release_lease("agent:turn:1", "not-the-holder")
    assert s._acquire_lease("agent:turn:1", 180) is None
    expire_lease("agent:turn:1")
    h2 = s._acquire_lease("agent:turn:1", 180)
    assert h2 and h2 != h
    s._release_lease("agent:turn:1", h)  # old holder: no effect
    assert s._acquire_lease("agent:turn:1", 180) is None
    s._release_lease("agent:turn:1", h2)
    assert s._acquire_lease("agent:turn:1", 180) is not None


async def test_async_wrappers_round_trip():
    await s.session_issue("A", "sid1", 7, TTL)
    assert await s.session_rotate("A", "B", "sid1", 7, TTL) == s.SESSION_ROTATE_OK
    assert (await s.session_grace("A"))["successor_jti"] == "B"
    assert await s.claim_token("x", "t", 5) is True
    assert await s.acquire_lease("n", 5) is not None


def test_ci_runs_the_mysql_fences_before_the_last_step():
    """The MySQL fences skip without a URL; CI must set it and fail on a skip.
    The last step of the job belongs to the platform reserve fences."""
    from pathlib import Path

    import yaml

    root = next(p for p in Path(__file__).resolve().parents if (p / ".github/workflows/test.yml").exists())
    steps = yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]["migrations"]["steps"]
    step = next(st for st in steps[:-1] if "test_state_db_mysql.py" in st.get("run", ""))
    assert step["env"] == {
        "RATE_LIMIT_MYSQL_URL": "${{ env.DATABASE_URL }}",
        "STATE_DB_TEST_URL": "${{ env.DATABASE_URL }}",
    }
    assert "pipefail" in step["run"] and "^SKIPPED" in step["run"]
