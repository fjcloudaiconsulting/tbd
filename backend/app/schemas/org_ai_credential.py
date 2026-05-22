"""Pydantic schemas for per-org AI provider credentials (PR1).

Write paths accept plaintext keys (``api_key`` / ``bearer_token``)
but the response shape NEVER returns plaintext or ciphertext — only
the last-4 + fingerprint + provider metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.org_ai_credential import AiProvider


LABEL_MAX_LENGTH = 120
API_KEY_MIN_LENGTH = 4
API_KEY_MAX_LENGTH = 4096
BASE_URL_MAX_LENGTH = 512


class OrgAICredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AiProvider
    api_key: str = Field(
        min_length=API_KEY_MIN_LENGTH, max_length=API_KEY_MAX_LENGTH
    )
    bearer_token: Optional[str] = Field(
        default=None, max_length=API_KEY_MAX_LENGTH
    )
    base_url: Optional[str] = Field(default=None, max_length=BASE_URL_MAX_LENGTH)
    label: Optional[str] = Field(default=None, max_length=LABEL_MAX_LENGTH)

    @model_validator(mode="after")
    def _check_provider_requirements(self):
        if self.provider in (AiProvider.OLLAMA, AiProvider.OPENAI_COMPATIBLE):
            if not self.base_url:
                raise ValueError(
                    "base_url is required for ollama and openai_compatible providers"
                )
        if self.provider != AiProvider.OLLAMA and self.bearer_token:
            raise ValueError(
                "bearer_token is only valid for the ollama provider"
            )
        return self


class OrgAICredentialUpdate(BaseModel):
    """Label-only update. Key rotation has its own endpoint."""

    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = Field(default=None, max_length=LABEL_MAX_LENGTH)


class OrgAICredentialRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(
        min_length=API_KEY_MIN_LENGTH, max_length=API_KEY_MAX_LENGTH
    )
    bearer_token: Optional[str] = Field(
        default=None, max_length=API_KEY_MAX_LENGTH
    )


class OrgAICredentialResponse(BaseModel):
    """Sanitized response shape — NEVER includes encrypted_* or plaintext."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    provider: AiProvider
    last_four: str
    key_fingerprint: str
    base_url: Optional[str]
    label: Optional[str]
    discovered_capabilities: Optional[list[str]] = None
    discovered_models: Optional[list[str]] = None
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    validation_error: Optional[str] = None
