"""OAuth 2.1 public clients registered by dynamic client registration (TBD-587).

Platform-global: a client belongs to no org. Registration is idempotent on
``metadata_key`` (sha256 of the canonical ``[client_name, sorted redirect
URIs]``, loopback ports dropped), so every user of one hosted connector
shares one row. A grant is an ``api_tokens`` row pointing here (RESTRICT);
the purge job deletes clients with no row that have been idle for 30 days.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import CHAR, JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(100), nullable=False)
    redirect_uris: Mapped[list] = mapped_column(JSON, nullable=False)
    metadata_key: Mapped[str] = mapped_column(CHAR(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    # Stamped by an approved consent only, never by the context read.
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
