"""SSRF defense on OrgAICredentialCreate.base_url.

Pins the threat-model fix from the architect review: a malicious org
admin must not be able to point ``base_url`` at the cloud metadata
service or any private/loopback/link-local literal IP, even though
they can configure the OAI-compatible adapter freely.

DNS names still pass — the v1 mitigation is literal-IP-only. The
DNS-rebinding residual is documented in the schema docstring.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.org_ai_credential import AiProvider
from app.schemas.org_ai_credential import OrgAICredentialCreate


@pytest.mark.parametrize(
    "blocked_url",
    [
        # Loopback literals (v4 + v6).
        "http://127.0.0.1:11434",
        "http://127.55.66.77/",
        "http://[::1]:8000",
        # Private RFC1918 literals.
        "http://10.0.0.5",
        "http://172.16.5.5",
        "http://192.168.1.1",
        # Link-local — covers AWS / GCP IMDS at 169.254.169.254.
        "http://169.254.0.1",
        "http://169.254.169.254/latest/meta-data/",
        # IPv6 link-local.
        "http://[fe80::1]",
        # Multicast / unspecified / reserved.
        "http://224.0.0.1",
        "http://0.0.0.0",
        # IPv4-mapped IPv6 of a loopback (should unwrap and be rejected).
        "http://[::ffff:127.0.0.1]",
    ],
)
def test_base_url_rejects_blocked_literal_ips(blocked_url: str) -> None:
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-test-1234",
            base_url=blocked_url,
        )


@pytest.mark.parametrize(
    "bad_scheme_url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "javascript:alert(1)",
    ],
)
def test_base_url_rejects_non_http_schemes(bad_scheme_url: str) -> None:
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-test-1234",
            base_url=bad_scheme_url,
        )


@pytest.mark.parametrize(
    "allowed_url",
    [
        # Public DNS — happy path.
        "https://api.example.com/v1",
        "http://example.com",
        # Private DNS names — operator responsibility (homelab / VPC).
        "http://ollama.internal:11434",
        "http://ollama-svc.local",
        "https://internal-ollama.example.com:8000",
    ],
)
def test_base_url_allows_public_and_private_dns(allowed_url: str) -> None:
    # Should NOT raise.
    OrgAICredentialCreate(
        provider=AiProvider.OPENAI_COMPATIBLE,
        api_key="sk-test-1234",
        base_url=allowed_url,
    )


def test_base_url_required_for_ollama_and_compatible() -> None:
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-test-1234",
            base_url=None,
        )


# ---------------------------------------------------------------------------
# Ollama no-key (LAN-only homelab mode) — spec line 37 + ~L219
# ---------------------------------------------------------------------------


def test_ollama_credential_accepts_missing_api_key() -> None:
    """Ollama POST without api_key must validate successfully."""
    cred = OrgAICredentialCreate(
        provider=AiProvider.OLLAMA,
        base_url="http://ollama.internal:11434",
    )
    assert cred.api_key is None


def test_ollama_credential_accepts_null_api_key() -> None:
    """Ollama POST with api_key=None must validate successfully."""
    cred = OrgAICredentialCreate(
        provider=AiProvider.OLLAMA,
        api_key=None,
        base_url="http://ollama.internal:11434",
    )
    assert cred.api_key is None


def test_non_ollama_credential_still_rejects_missing_api_key() -> None:
    """Non-Ollama providers require api_key; omitting it must raise."""
    for provider in (AiProvider.OPENAI, AiProvider.ANTHROPIC):
        with pytest.raises(ValidationError):
            OrgAICredentialCreate(provider=provider)


def test_non_ollama_credential_still_rejects_short_api_key() -> None:
    """Non-Ollama providers reject api_key shorter than API_KEY_MIN_LENGTH."""
    for provider in (AiProvider.OPENAI, AiProvider.ANTHROPIC):
        with pytest.raises(ValidationError):
            OrgAICredentialCreate(provider=provider, api_key="abc")
