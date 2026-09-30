"""A previewed agent write awaiting confirmation (TBD-577).

One row per preview. The row is the whole audit trail of what the agent asked
for (``args_json``), what the server computed it would do (``preview_json``,
``fingerprint``) and what became of it (``status``, ``result_json``,
``error_code``).

* ``channel`` + ``api_token_id`` are the PRINCIPAL that staged the row and the
  only principal that may confirm or cancel it. In-app rows carry a NULL
  token, so every comparison on it must be NULL-safe.
* No ``expired`` status and no sweep: expiry is ``expires_at <= now`` on a
  ``pending`` row. Timestamps are naive UTC, written app-side so SQLite and
  MySQL compare alike.
* ``api_token_id`` is ``ON DELETE SET NULL`` and is redacted from the data
  export for the reason ``audit_events.api_token_id`` is (it points into a
  table the subject cannot see).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CHAR, JSON, BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda x: [e.value for e in x])


class ActionChannel(str, enum.Enum):
    IN_APP = "in_app"
    MCP = "mcp"


class ActionRisk(str, enum.Enum):
    WRITE = "write"
    SENSITIVE = "sensitive"


class ActionMode(str, enum.Enum):
    CONFIRM = "confirm"
    AUTO = "auto"


class ActionStatus(str, enum.Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"


class AgentPendingAction(Base):
    __tablename__ = "agent_pending_actions"
    __table_args__ = (
        Index("ix_agent_pending_actions_org_created", "org_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[ActionChannel] = mapped_column(
        _enum(ActionChannel, "agent_action_channel"), nullable=False
    )
    # BigInteger mirrors ``api_tokens.id`` (MySQL rejects an FK whose type
    # differs); the SQLite variant keeps the test path on INTEGER affinity.
    api_token_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("api_tokens.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    risk: Mapped[ActionRisk] = mapped_column(_enum(ActionRisk, "agent_action_risk"), nullable=False)
    mode: Mapped[ActionMode] = mapped_column(
        _enum(ActionMode, "agent_action_mode"), nullable=False, default=ActionMode.CONFIRM
    )
    args_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    args_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[ActionStatus] = mapped_column(
        _enum(ActionStatus, "agent_action_status"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
