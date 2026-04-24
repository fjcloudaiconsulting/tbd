from datetime import datetime, timedelta, timezone

from app.models.user import Role, User
from app.security import (
    create_access_token,
    create_mfa_email_token,
    create_refresh_token,
    decode_token,
    hash_password,
    token_cutoff,
    verify_password,
)


def make_user(**overrides) -> User:
    base = {
        "org_id": 1,
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "hashed-password",
        "role": Role.OWNER,
        "is_superadmin": False,
    }
    base.update(overrides)
    return User(**base)


def test_hash_password_roundtrip_verifies_plaintext() -> None:
    hashed = hash_password("correct horse battery staple")

    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password", hashed) is False


def test_create_access_token_roundtrip_decodes_expected_claims() -> None:
    token = create_access_token(subject=7, org_id=3, role="owner")
    payload = decode_token(token)

    assert payload is not None
    assert payload["sub"] == "7"
    assert payload["org_id"] == 3
    assert payload["role"] == "owner"
    assert payload["type"] == "access"


def test_refresh_token_preserves_original_session_created_at() -> None:
    session_start = datetime.now(timezone.utc) - timedelta(days=2)

    token = create_refresh_token(subject=5, session_created_at=session_start)
    payload = decode_token(token)

    assert payload is not None
    assert payload["type"] == "refresh"
    assert payload["sub"] == "5"
    assert payload["session_created_at"] == session_start.timestamp()


def test_refresh_token_defaults_session_created_at_to_now() -> None:
    before = datetime.now(timezone.utc).timestamp()
    token = create_refresh_token(subject=9)
    after = datetime.now(timezone.utc).timestamp()
    payload = decode_token(token)

    assert payload is not None
    assert before <= payload["session_created_at"] <= after


def test_create_mfa_email_token_bakes_hmac_and_jti_into_token() -> None:
    token, jti = create_mfa_email_token(user_id=11, code="482913")
    payload = decode_token(token)

    assert payload is not None
    assert payload["sub"] == "11"
    assert payload["type"] == "mfa_email"
    assert payload["jti"] == jti
    assert payload["code_hmac"] != "482913"


def test_decode_token_returns_none_for_tampered_token() -> None:
    token = create_access_token(subject=7, org_id=3, role="owner")
    tampered = f"{token}x"

    assert decode_token(tampered) is None


def test_token_cutoff_uses_latest_of_password_and_session_invalidation() -> None:
    password_changed_at = datetime(2026, 4, 20, 8, 0, 0)
    sessions_invalidated_at = datetime(2026, 4, 22, 9, 30, 0, tzinfo=timezone.utc)
    user = make_user(
        password_changed_at=password_changed_at,
        sessions_invalidated_at=sessions_invalidated_at,
    )

    assert token_cutoff(user) == sessions_invalidated_at


def test_token_cutoff_returns_datetime_min_when_user_has_no_cutoffs() -> None:
    user = make_user()

    assert token_cutoff(user) == datetime.min.replace(tzinfo=timezone.utc)


def test_token_cutoff_normalizes_naive_session_invalidation_timestamp() -> None:
    naive_cutoff = datetime(2026, 4, 23, 7, 45, 0)
    user = make_user(sessions_invalidated_at=naive_cutoff)

    assert token_cutoff(user) == naive_cutoff.replace(tzinfo=timezone.utc)
