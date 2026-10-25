"""SSRF defense on OrgAICredentialCreate.base_url.

Pins the threat-model fix from the architect review: a malicious org
admin must not be able to point ``base_url`` at the cloud metadata
service or any private/loopback/link-local literal IP, even though
they can configure the OAI-compatible adapter freely.

DNS names still pass at save time — this layer is literal-IP
fast-feedback defense-in-depth. The enforcement point is the
connect-time guard (``services.ai_providers.egress_guard``), which
resolves DNS names, validates every record, and pins the connection
(see tests/services/test_ai_provider_egress_guard.py). No provider has a
LAN/loopback exemption: the only one, for ``ollama``, left with it in
TBD-584.
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
        # Private DNS names pass here; the connect-time guard refuses
        # them when they resolve to a non-public address.
        "http://llm.internal:11434",
        "http://llm-svc.local",
        "https://internal-llm.example.com:8000",
    ],
)
def test_base_url_allows_public_and_private_dns(allowed_url: str) -> None:
    # Should NOT raise.
    OrgAICredentialCreate(
        provider=AiProvider.OPENAI_COMPATIBLE,
        api_key="sk-test-1234",
        base_url=allowed_url,
    )


def test_base_url_required_for_compatible() -> None:
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-test-1234",
            base_url=None,
        )


@pytest.mark.parametrize("provider", list(AiProvider))
def test_every_provider_rejects_missing_api_key(provider) -> None:
    """No provider takes a keyless credential since the ollama LAN-only
    mode left (TBD-584)."""
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=provider,
            **({"base_url": "https://llm.example.com/v1"} if provider == AiProvider.OPENAI_COMPATIBLE else {}),
        )


@pytest.mark.parametrize("provider", list(AiProvider))
def test_every_provider_rejects_short_api_key(provider) -> None:
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=provider,
            api_key="abc",
            **({"base_url": "https://llm.example.com/v1"} if provider == AiProvider.OPENAI_COMPATIBLE else {}),
        )


@pytest.mark.parametrize("provider", ["ollama", "native"])
def test_removed_provider_is_refused(provider) -> None:
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=provider,
            api_key="sk-test-key-1234",
            base_url="https://llm.example.com/",
        )


def test_bearer_token_is_no_longer_accepted() -> None:
    """``bearer_token`` served only the ollama reverse-proxy mode."""
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-test-key-1234",
            base_url="https://llm.example.com/",
            bearer_token="tok-1234",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.168.1.163:11434/",   # RFC1918 192.168/16
        "http://10.0.0.5:11434/",        # RFC1918 10/8
        "http://172.16.5.5:11434/",      # RFC1918 172.16/12
        "http://127.0.0.1:11434/",       # loopback IPv4
        "http://[::1]:11434/",           # loopback IPv6
        "http://[::ffff:192.168.1.1]/",  # IPv4-mapped IPv6 LAN
        "http://169.254.169.254/",       # AWS/GCP/DO IMDS
        "http://[fd00:ec2::254]/",       # AWS IPv6 IMDS
        "http://224.0.0.1/",             # multicast
    ],
)
def test_openai_compatible_rejects_local_endpoints(base_url: str) -> None:
    """A local model server cannot come back through openai_compatible."""
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-test-key-1234",
            base_url=base_url,
        )


def test_public_ip_still_accepted() -> None:
    cred = OrgAICredentialCreate(
        provider=AiProvider.OPENAI_COMPATIBLE,
        api_key="sk-test-key-1234",
        base_url="https://api.example.com/",
    )
    assert cred.base_url.endswith(".example.com/")
