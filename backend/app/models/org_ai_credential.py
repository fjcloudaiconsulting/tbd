"""Per-org AI provider credential (PR1 of AI tier train).

One row per provider connection. Plaintext keys NEVER reach disk —
only the Fernet token does. The ``__repr__`` masks ``last_four`` so
log captures of model instances don't trip a secret scanner.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AiProvider(str, enum.Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    NATIVE = "native"


class OrgAICredential(Base):
    __tablename__ = "org_ai_credentials"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "id", name="uq_org_ai_credentials_org_id_id"
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
    provider: Mapped[AiProvider] = mapped_column(
        Enum(
            AiProvider,
            name="ai_provider",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_bearer_token: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    base_url: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    last_four: Mapped[str] = mapped_column(String(8), nullable=False)
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

    def __repr__(self) -> str:  # pragma: no cover - debug repr
        provider = self.provider.value if self.provider else None
        masked = f"***{self.last_four}" if self.last_four else "***"
        return (
            f"<OrgAICredential id={self.id} provider={provider} "
            f"last_four={masked}>"
        )
