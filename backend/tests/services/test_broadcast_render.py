"""Tests for per-recipient render + materialization (Task 3, spec
``2026-07-18-admin-email-broadcast-design.md``).

Covers:
- ``render_email`` escapes the recipient name AND the body template for
  the HTML path (Ruling 11), while the text path stays raw.
- ``render_email`` falls back to "there" when ``first_name`` is ``None``.
- Substitution uses ``str.replace``, never ``str.format()`` (Ruling 11),
  so a stray ``{`` in operator copy does not raise.
- ``materialize_recipients`` snapshots the segment into PENDING recipient
  rows and sets ``broadcast.total_recipients``.

Uses an in-memory aiosqlite engine (same fixture pattern as
``tests/services/test_broadcast_segment.py``) so no running MySQL /
docker-compose stack is required.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.email_broadcast import (
    SEGMENT_ACTIVE_VERIFIED,
    EmailBroadcast,
    EmailBroadcastRecipient,
    RecipientStatus,
)
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services.broadcast_service import (
    build_batch_bodies,
    build_recipient_variables,
    materialize_recipients,
    render_email,
)


def _simulate_mailgun(body_with_tokens: str, first_name):
    """Substitute Mailgun's %recipient.*% tokens the way the batch send
    relies on Mailgun doing at delivery time, using the SAME vars builder
    the drain uses (so the parity check can't drift from real behaviour)."""
    variables = build_recipient_variables([("x@example.com", first_name)])["x@example.com"]
    return body_with_tokens.replace(
        "%recipient.first_name_html%", variables["first_name_html"]
    ).replace("%recipient.first_name_text%", variables["first_name_text"])


def test_render_email_formats_paragraphs_and_line_breaks():
    """A blank line starts a new <p>; a single newline becomes <br>.
    Regression for the run-on-block bug in the first dry-run."""
    body = "Hi {first_name},\n\nFirst paragraph.\n\nSign off,\nThe Team"
    html_out, text_out = render_email(body, "Alex")
    # greeting, para 1, sign-off, plus the footer = 4 <p> blocks
    assert html_out.count("<p>") == 4
    assert "<p>Hi Alex,</p>" in html_out
    assert "<p>First paragraph.</p>" in html_out
    assert "<p>Sign off,<br>The Team</p>" in html_out
    # NOT a single run-on paragraph
    assert "First paragraph.\n\nSign off" not in html_out
    # text part keeps the raw newlines
    assert "First paragraph.\n\nSign off,\nThe Team" in text_out


def test_batch_bodies_match_render_email_with_paragraphs():
    """MA3 byte-parity holds for a multi-paragraph body: the batch HTML,
    once Mailgun substitutes the recipient token, equals render_email."""
    body = "Hi {first_name},\n\nLine one.\n\nLine two.\nStill two."
    html_tokens, text_tokens = build_batch_bodies(body)
    for name in ("Alex", None, "A<b>&"):
        assert _simulate_mailgun(html_tokens, name) == render_email(body, name)[0]
        assert _simulate_mailgun(text_tokens, name) == render_email(body, name)[1]


def test_render_email_escapes_name_and_body_for_html():
    html_out, text_out = render_email("Hi {first_name}, welcome", "A<b>&")

    assert "A&lt;b&gt;&amp;" in html_out
    assert "<b>" not in html_out
    assert "A<b>&" in text_out


def test_render_email_falls_back_to_there_when_no_first_name():
    html_out, text_out = render_email("Hi {first_name},", None)

    assert "Hi there," in html_out
    assert "Hi there," in text_out


def test_render_email_footer_present_in_both_parts():
    html_out, text_out = render_email("Body copy", "Alice")

    footer = "You're receiving this because you have a The Better Decision account."
    assert footer in html_out
    assert footer in text_out


def test_render_email_stray_brace_does_not_raise():
    # A stray `{` (e.g. operator typed a literal brace) must never raise
    # KeyError / ValueError the way str.format() would.
    html_out, text_out = render_email(
        "Cost is {5} for {first_name} today", "Alice"
    )

    assert "Alice" in html_out
    assert "Alice" in text_out


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_with_users(session_factory):
    """Seed an Org + 2 active+verified Users (the ``active_verified`` segment)."""
    async with session_factory() as db:
        org = Organization(name="TestOrg", billing_cycle_day=1)
        db.add(org)
        await db.flush()

        db.add_all(
            [
                User(
                    username="alice",
                    email="alice@x.io",
                    first_name="Alice",
                    password_hash=hash_password("pw-1234567"),
                    org_id=org.id,
                    role=Role.OWNER,
                    is_active=True,
                    email_verified=True,
                ),
                User(
                    username="bob",
                    email="bob@x.io",
                    first_name="Bob",
                    password_hash=hash_password("pw-1234567"),
                    org_id=org.id,
                    role=Role.MEMBER,
                    is_active=True,
                    email_verified=True,
                ),
            ]
        )
        await db.commit()
        yield db


@pytest.mark.asyncio
async def test_materialize_recipients_snapshots_segment(db_with_users):
    broadcast = EmailBroadcast(
        subject="Hi",
        body_template="Hi {first_name},",
        segment=SEGMENT_ACTIVE_VERIFIED,
    )
    db_with_users.add(broadcast)
    await db_with_users.flush()

    count = await materialize_recipients(db_with_users, broadcast)
    await db_with_users.commit()

    assert count == 2
    assert broadcast.total_recipients == 2

    result = await db_with_users.execute(
        select(EmailBroadcastRecipient).where(
            EmailBroadcastRecipient.broadcast_id == broadcast.id
        )
    )
    recipients = result.scalars().all()
    assert len(recipients) == 2
    assert {r.email for r in recipients} == {"alice@x.io", "bob@x.io"}
    assert all(r.status == RecipientStatus.PENDING for r in recipients)
