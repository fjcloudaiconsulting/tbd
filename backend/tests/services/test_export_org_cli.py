"""Operator CLI fences (TBD-222 §7).

The CLI's job beyond streaming is a single safety property: **a partial file
never appears under the final name.** It writes ``<out>.part`` and renames
only after the trailer has flushed, so a crashed run leaves either nothing at
the destination or a complete artifact — and the ``.part`` it does leave has
no terminal trailer, so ``verify_export`` rejects it.

That is the same truncation signal as the stream-level fence, observed at the
filesystem instead of in memory.
"""
from __future__ import annotations

import argparse
import json

import pytest

from app.services.org_export_service import verify_export
from scripts import export_org

from tests.services.test_export_registry import (  # noqa: F401  (fixtures)
    session_factory,
    two_orgs,
)


def _args(**kwargs) -> argparse.Namespace:
    base = dict(
        org_id=1,
        out=None,
        stdout=False,
        dry_run=False,
        operator="operator@example.test",
        max_rows=None,
        max_bytes=None,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


@pytest.fixture
def cli(monkeypatch, two_orgs):
    """Point the CLI's module-global session factory at the sqlite fixture."""
    monkeypatch.setattr(export_org, "async_session", two_orgs["factory"])
    return two_orgs


# ══ Happy path ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_writes_a_verifiable_artifact_and_removes_the_part_file(cli, tmp_path):
    out = tmp_path / "org.ndjson"
    code = await export_org._run(_args(org_id=cli["a"]["org_id"], out=str(out)))

    assert code == 0
    assert out.exists()
    assert not (tmp_path / "org.ndjson.part").exists(), ".part must be renamed away"

    with open(out, "rb") as handle:
        result = verify_export(handle)
    assert result.ok, result.reason
    assert result.total_rows > 0


@pytest.mark.asyncio
async def test_writes_an_audit_row_carrying_sha256_and_row_counts(cli, tmp_path):
    """⚠ ``sha256`` and ``row_counts_by_table`` are load-bearing: they are
    what later proves the artifact handed to a data subject matches what was
    generated. Row COUNTS, never row contents."""
    from sqlalchemy import select

    from app.models.audit_event import AuditEvent

    out = tmp_path / "org.ndjson"
    await export_org._run(_args(org_id=cli["a"]["org_id"], out=str(out)))

    async with cli["factory"]() as db:
        event = (
            await db.execute(
                select(AuditEvent).where(AuditEvent.event_type == "org.data.exported")
            )
        ).scalar_one()

    detail = event.detail
    assert event.target_org_id == cli["a"]["org_id"]
    assert detail["sha256"] == json.loads(out.read_bytes().splitlines()[-1])["sha256_body"]
    assert detail["row_counts_by_table"]["transactions"] == 1
    assert detail["byte_size"] == out.stat().st_size
    assert "tag_dictionary_contributors" in detail["excluded_tables"]

    # Counts only — no row payloads may ride along in the audit detail.
    assert "TRANSACTION-AAA" not in json.dumps(detail)


# ══ The partial-file property ═════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_killed_run_leaves_no_file_at_the_destination(
    cli, tmp_path, monkeypatch
):
    """Simulate the process dying mid-stream.

    The destination must not exist, and the ``.part`` left behind must fail
    verification for the same reason any truncated artifact does: no trailer.
    """
    real_stream = export_org.stream_org_export

    async def _dying_stream(*args, **kwargs):
        emitted = 0
        async for line in real_stream(*args, **kwargs):
            yield line
            emitted += 1
            if emitted >= 5:
                raise RuntimeError("simulated process death mid-table")

    monkeypatch.setattr(export_org, "stream_org_export", _dying_stream)

    out = tmp_path / "org.ndjson"
    with pytest.raises(RuntimeError, match="simulated process death"):
        await export_org._run(_args(org_id=cli["a"]["org_id"], out=str(out)))

    assert not out.exists(), "a partial file must never appear under the final name"

    part = tmp_path / "org.ndjson.part"
    assert part.exists()
    lines = part.read_bytes().splitlines(keepends=True)
    assert not any(json.loads(raw)["record"] == "trailer" for raw in lines)
    result = verify_export(lines)
    assert result.ok is False
    assert "trailer" in (result.reason or "")


@pytest.mark.asyncio
async def test_a_refused_oversize_export_leaves_nothing_behind(cli, tmp_path):
    out = tmp_path / "org.ndjson"
    code = await export_org._run(
        _args(org_id=cli["a"]["org_id"], out=str(out), max_rows=1)
    )

    assert code == 3
    assert not out.exists()
    assert not (tmp_path / "org.ndjson.part").exists()


# ══ Argument handling ═════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unknown_org_exits_nonzero_without_writing(cli, tmp_path):
    out = tmp_path / "org.ndjson"
    code = await export_org._run(_args(org_id=999_999, out=str(out)))
    assert code == 2
    assert not out.exists()


@pytest.mark.asyncio
async def test_dry_run_reports_counts_and_writes_nothing(cli, tmp_path, capsys):
    out = tmp_path / "org.ndjson"
    code = await export_org._run(
        _args(org_id=cli["a"]["org_id"], out=str(out), dry_run=True)
    )
    assert code == 0
    assert not out.exists()
    assert "transactions" in capsys.readouterr().err


def test_out_and_stdout_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        export_org.main(["--org-id", "1", "--out", "/tmp/x", "--stdout"])


def test_a_destination_is_required():
    with pytest.raises(SystemExit):
        export_org.main(["--org-id", "1"])
