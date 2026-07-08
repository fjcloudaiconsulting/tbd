"""SSRF defense on OrgAICredentialCreate.base_url.

Pins the threat-model fix from the architect review: a malicious org
admin must not be able to point ``base_url`` at the cloud metadata
service or any private/loopback/link-local literal IP, even though
they can configure the OAI-compatible adapter freely.

DNS names still pass at save time — this layer is literal-IP
fast-feedback defense-in-depth. The enforcement point is the
connect-time guard (``services.ai_providers.egress_guard``), which
resolves DNS names, validates every record, and pins the connection
(see tests/services/test_ai_provider_egress_guard.py). The Ollama
LAN/loopback exemption is gated on the
``AI_PROVIDER_ALLOW_PRIVATE_NETWORKS`` setting (default OFF).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import settings
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
    for provider in (AiProvider.OPENAI, AiProvider.ANTHROPIC, AiProvider.OPENAI_COMPATIBLE):
        with pytest.raises(ValidationError):
            OrgAICredentialCreate(
                provider=provider,
                **({"base_url": "https://localhost:11434/v1"} if provider == AiProvider.OPENAI_COMPATIBLE else {}),
            )


def test_non_ollama_credential_still_rejects_short_api_key() -> None:
    """Non-Ollama providers reject api_key shorter than API_KEY_MIN_LENGTH."""
    for provider in (AiProvider.OPENAI, AiProvider.ANTHROPIC, AiProvider.OPENAI_COMPATIBLE):
        with pytest.raises(ValidationError):
            OrgAICredentialCreate(
                provider=provider,
                api_key="abc",
                **({"base_url": "https://localhost:11434/v1"} if provider == AiProvider.OPENAI_COMPATIBLE else {}),
            )


# --- 2026-05-29 Ollama LAN/loopback policy, re-gated 2026-07-07 behind
# --- AI_PROVIDER_ALLOW_PRIVATE_NETWORKS (default OFF).

_OLLAMA_LAN_URLS = [
    "http://192.168.1.163:11434/",   # RFC1918 192.168/16
    "http://10.0.0.5:11434/",        # RFC1918 10/8
    "http://172.16.5.5:11434/",      # RFC1918 172.16/12
    "http://127.0.0.1:11434/",       # loopback IPv4
    "http://[::1]:11434/",           # loopback IPv6
    "http://[::ffff:192.168.1.1]/",  # IPv4-mapped IPv6 LAN
]


@pytest.mark.parametrize("base_url", _OLLAMA_LAN_URLS)
def test_ollama_accepts_lan_and_loopback_ip_with_flag_on(
    base_url: str, monkeypatch
) -> None:
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", True
    )
    cred = OrgAICredentialCreate(
        provider=AiProvider.OLLAMA,
        base_url=base_url,
    )
    assert cred.base_url == base_url


@pytest.mark.parametrize("base_url", _OLLAMA_LAN_URLS)
def test_ollama_rejects_lan_and_loopback_ip_by_default(
    base_url: str, monkeypatch
) -> None:
    """Flag OFF (the default everywhere): Ollama gets the same full
    denylist as every other provider."""
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", False
    )
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OLLAMA,
            base_url=base_url,
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254/",                   # AWS/GCP/DO IMDS
        "http://[fd00:ec2::254]/",                   # AWS IPv6 IMDS
        "http://[::ffff:169.254.169.254]/",          # mapped IPv6 metadata
        "http://169.254.1.1/",                       # link-local non-metadata
        "http://224.0.0.1/",                         # multicast
    ],
)
def test_ollama_still_rejects_metadata_and_unsafe(
    base_url: str, monkeypatch
) -> None:
    """Blocked even with the private-networks escape hatch enabled."""
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", True
    )
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OLLAMA,
            base_url=base_url,
        )


def test_openai_compatible_still_rejects_lan_ip() -> None:
    """Strict SSRF block unchanged for non-Ollama providers."""
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-test-key-1234",
            base_url="http://192.168.1.163/",
        )


def test_public_ip_still_accepted_for_any_provider() -> None:
    cred_ollama = OrgAICredentialCreate(
        provider=AiProvider.OLLAMA,
        base_url="https://ollama.example.com/",
    )
    cred_openai = OrgAICredentialCreate(
        provider=AiProvider.OPENAI_COMPATIBLE,
        api_key="sk-test-key-1234",
        base_url="https://api.example.com/",
    )
    assert cred_ollama.base_url.endswith(".example.com/")
    assert cred_openai.base_url.endswith(".example.com/")
