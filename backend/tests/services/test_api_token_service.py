"""Hashing helpers for superadmin Personal Access Tokens (PAT). Plaintext is
never stored; only an HMAC-SHA256 hash under a decoupled pepper
(``API_TOKEN_HMAC_KEY``) is persisted. See app/services/api_token_service.py.
"""

from app.services import api_token_service as svc


def test_generate_token_shape():
    full, h, prefix = svc.generate_token()
    assert full.startswith("pat_")
    assert prefix == full[:14] and prefix.startswith("pat_")
    assert h == svc.hash_api_token(full)
    assert len(h) == 64  # sha256 hex


def test_hash_is_deterministic_and_keyed(monkeypatch):
    a = svc.hash_api_token("pat_abc")
    assert a == svc.hash_api_token("pat_abc")


def test_prev_key_candidate_included(monkeypatch):
    monkeypatch.setattr(svc.settings, "api_token_hmac_key_prev", "p" * 40, raising=False)
    cands = svc.token_hash_candidates("pat_abc")
    assert len(cands) == 2 and cands[0] == svc.hash_api_token("pat_abc")


def test_no_prev_key_single_candidate(monkeypatch):
    monkeypatch.setattr(svc.settings, "api_token_hmac_key_prev", None, raising=False)
    assert len(svc.token_hash_candidates("pat_abc")) == 1
