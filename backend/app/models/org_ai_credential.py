"""Per-org AI provider credential (PR1 of AI tier train).

One row per provider connection. Plaintext keys NEVER reach disk —
only the Fernet token does. The ``__repr__`` masks ``last_four`` so
log captures of model instances don't trip a secret scanner.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional, Union

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, validates
from sqlalchemy.types import TypeDecorator

from app.models.base import Base


class AiProvider(str, enum.Enum):
    """The provider registry: every value the app can dispatch to.

    TBD-584: ``ollama`` (bring-your-own local model) and the dead ``native``
    stub left the registry, and migration 096 deletes the ``ollama`` rows.
    The column is VARCHAR, so a row that still holds one of them (a pod of
    the previous release can write one during the rolling deploy, after 096
    ran) loads as a plain ``str`` (see ``AiProviderType``) and stays inert:
    listed and deletable, never validated, routed or dispatched.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"


PROVIDER_MAX_LENGTH = 32


class AiProviderType(TypeDecorator):
    """``org_ai_credentials.provider`` as VARCHAR(32) (TBD-584).

    A registry value loads as its ``AiProvider`` member; any other stored
    value (a retired provider) loads as the plain string, so reading a row
    never raises. Adding a vendor needs no DDL, and removing one cannot crash
    a reader that does not know the value, as a native ENUM column would.
    Writes are checked by ``OrgAICredential._check_provider``.
    """

    impl = String(PROVIDER_MAX_LENGTH)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, AiProvider):
            return value.value
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        try:
            return AiProvider(value)
        except ValueError:
            return value


def is_supported_provider(value: Any) -> bool:
    """True when ``value`` is a provider the app can still dispatch to."""
    return isinstance(value, AiProvider)


def provider_key(value: Any) -> Optional[str]:
    """The stored string of a provider, registry member or retired value."""
    if value is None:
        return None
    return value.value if isinstance(value, AiProvider) else str(value)


PLATFORM_PROVIDER = Enum(
    "openrouter", "openai", "anthropic", "gemini", name="ai_platform_provider"
)


class OrgAICredential(Base):
    __tablename__ = "org_ai_credentials"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "id", name="uq_org_ai_credentials_org_id_id"
        ),
        UniqueConstraint(
            "org_id", "platform_provider", name="uq_org_ai_credentials_org_platform"
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # TBD-584: VARCHAR, validated against ``AiProvider`` on write. A row
    # holding a retired provider reads back as ``str``.
    provider: Mapped[Union[AiProvider, str]] = mapped_column(
        AiProviderType(), nullable=False
    )
    # TBD-586: set only on a platform (house-key) row; NULL on every BYOK row.
    platform_provider: Mapped[Optional[str]] = mapped_column(
        PLATFORM_PROVIDER, nullable=True
    )
    encrypted_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_bearer_token: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    base_url: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    # TBD-590: True means ``base_url`` is the API root including its version
    # and the adapter appends only the endpoint. Rows created before it are
    # False and keep the legacy ``{base_url}/v1`` root. ``base_url`` is
    # immutable after create, so only the create path writes True.
    base_url_is_api_root: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0", default=False
    )
    key_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_four: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    discovered_capabilities: Mapped[Optional[list[str]]] = mapped_column(
        JSON, nullable=True
    )
    discovered_models: Mapped[Optional[list[str]]] = mapped_column(
        JSON, nullable=True
    )
    label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_validated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    validation_error: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )

    @validates("provider")
    def _check_provider(self, _key: str, value: Any) -> AiProvider:
        # Only writes pass here (a load does not): a retired or unknown
        # provider can never be stored again.
        return AiProvider(value)

    def __repr__(self) -> str:  # pragma: no cover - debug repr
        provider = provider_key(self.provider)
        masked = f"***{self.last_four}" if self.last_four else "***"
        return (
            f"<OrgAICredential id={self.id} provider={provider} "
            f"last_four={masked}>"
        )
