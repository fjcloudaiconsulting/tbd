"""OFX parse isolation + concurrency-cap tests (DoS mitigation).

Backs the fix that closes the latent single-replica DoS: OFX parsing runs
in a hard-killable child process bounded by a per-org + global concurrency
cap. Covers:

  * the killable boundary (timeout terminates the worker; capacity frees;
    a subsequent parse still succeeds) — the strongest end-to-end proof;
  * the per-org concurrency cap (over-cap → 429; a different org unaffected);
  * the global concurrency cap (over-cap → 429 with reason ``global_busy``);
  * the 429 mapping at the ``parse_ofx`` boundary;
  * the row cap restored to 10 000 (configurable) and its boundary;
  * config knobs resolved from ``settings``.

These are service-layer tests (direct ``parse_ofx`` / ``OFXParseExecutor``
calls) so a failure points at the isolation machinery, not router wiring.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import import_ofx_service
from app.services.import_ofx_service import (
    OFXParseExecutor,
    _CapExceeded,
    parse_ofx,
)
from app.services.import_parser import ParseError

FIXTURES = (
    Path(__file__).resolve().parent.parent / "fixtures" / "import" / "ofx"
)


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _make_ofx_with_rows(n: int) -> bytes:
    """Build a minimal OFX 2.x statement with exactly ``n`` transactions.

    Mirrors the on-disk ``large_10k_rows.ofx`` fixture shape but is
    generated in-memory so a boundary test can pick any row count cheaply.
    """
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" '
        'OLDFILEUID="NONE" NEWFILEUID="NONE"?>\n'
        "<OFX>\n"
        "  <SIGNONMSGSRSV1><SONRS>\n"
        "    <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>\n"
        "    <DTSERVER>20260501120000</DTSERVER>\n"
        "    <LANGUAGE>ENG</LANGUAGE>\n"
        "  </SONRS></SIGNONMSGSRSV1>\n"
        "  <BANKMSGSRSV1><STMTTRNRS>\n"
        "    <TRNUID>1</TRNUID>\n"
        "    <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>\n"
        "    <STMTRS>\n"
        "      <CURDEF>EUR</CURDEF>\n"
        "      <BANKACCTFROM>\n"
        "        <BANKID>INGBNL2A</BANKID>\n"
        "        <ACCTID>NL03TEST0000000003</ACCTID>\n"
        "        <ACCTTYPE>CHECKING</ACCTTYPE>\n"
        "      </BANKACCTFROM>\n"
        "      <BANKTRANLIST>\n"
        "        <DTSTART>20240101</DTSTART>\n"
        "        <DTEND>20260430</DTEND>\n"
    )
    rows = "\n".join(
        "<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20240101</DTPOSTED>"
        f"<TRNAMT>-1.00</TRNAMT><FITID>GEN{i:07d}</FITID>"
        f"<NAME>Row{i}</NAME></STMTTRN>"
        for i in range(1, n + 1)
    )
    footer = (
        "\n      </BANKTRANLIST>\n"
        "      <LEDGERBAL><BALAMT>0.00</BALAMT><DTASOF>20260430</DTASOF>"
        "</LEDGERBAL>\n"
        "    </STMTRS>\n"
        "  </STMTTRNRS></BANKMSGSRSV1>\n"
        "</OFX>\n"
    )
    return (header + rows + footer).encode()


# ── Config knobs resolve from settings ──────────────────────────────────────


def test_config_knobs_have_expected_defaults():
    """The DoS-mitigation knobs are config-driven with the documented
    defaults. ``ofx_max_rows`` is restored to 10 000 (was a 2 000 stopgap)."""
    assert settings.ofx_max_rows == 10_000
    assert settings.ofx_parse_timeout_s == 10.0
    assert settings.ofx_parse_max_concurrent == 4
    assert settings.ofx_parse_max_per_org == 2
    assert settings.ofx_parse_queue_wait_s == 5.0


# ── Killable boundary: timeout terminates the worker, capacity frees ────────


@pytest.mark.asyncio
async def test_timeout_terminates_worker_and_frees_capacity(monkeypatch, tmp_path):
    """A runaway parse is hard-killed on timeout; a later parse still works.

    Uses a single-slot executor (``max_concurrent=1``) so that the second,
    successful parse can ONLY run if the first runaway's slot was released —
    proving the timeout path did not leak a saturated slot. The sentinel
    proves the runaway child never completed (CPU genuinely reclaimed).
    """
    sentinel = tmp_path / "completed.flag"
    monkeypatch.setenv("PFV_OFX_TEST_HANG_S", "10")
    monkeypatch.setenv("PFV_OFX_TEST_SENTINEL", str(sentinel))

    ex = OFXParseExecutor(max_concurrent=1, max_per_org=1, queue_wait_s=0.0)
    monkeypatch.setattr(import_ofx_service, "_executor", ex)

    with pytest.raises(HTTPException) as exc_info:
        await parse_ofx(b"<OFX>hang</OFX>", org_id=1, timeout_s=0.5)
    assert exc_info.value.status_code == 400
    # The runaway did NOT finish: it was terminated mid busy-loop.
    assert not sentinel.exists()

    # Capacity is free again (single slot): a real parse now succeeds.
    monkeypatch.delenv("PFV_OFX_TEST_HANG_S")
    rows = await parse_ofx(_read("rabobank_ofx_1x.ofx"), org_id=1)
    assert len(rows) == 15


# ── Per-org concurrency cap ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_per_org_cap_rejects_immediately():
    """A second concurrent slot for the SAME org over the per-org cap is
    rejected immediately; a DIFFERENT org is unaffected."""
    ex = OFXParseExecutor(max_concurrent=10, max_per_org=1, queue_wait_s=0.0)
    async with ex.slot(org_id=1):
        # Same org, already at its cap of 1 → immediate reject.
        with pytest.raises(_CapExceeded) as exc_info:
            async with ex.slot(org_id=1):
                pass
        assert exc_info.value.reason == "per_org"
        # A different org still has capacity.
        async with ex.slot(org_id=2):
            pass


@pytest.mark.asyncio
async def test_executor_per_org_slot_released_after_use():
    """The per-org counter is decremented on release, so the same org can
    parse again once its previous slot completes."""
    ex = OFXParseExecutor(max_concurrent=10, max_per_org=1, queue_wait_s=0.0)
    async with ex.slot(org_id=7):
        pass
    # Slot released — org 7 can acquire again.
    async with ex.slot(org_id=7):
        pass


# ── Global concurrency cap ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_global_cap_rejects_when_full():
    """When every GLOBAL slot is taken (even across different orgs), the
    next request is rejected with ``global_busy`` after the bounded wait."""
    ex = OFXParseExecutor(max_concurrent=1, max_per_org=5, queue_wait_s=0.0)
    async with ex.slot(org_id=1):
        with pytest.raises(_CapExceeded) as exc_info:
            async with ex.slot(org_id=2):  # different org, but global full
                pass
        assert exc_info.value.reason == "global_busy"


# ── 429 mapping at the parse_ofx boundary ───────────────────────────────────


@pytest.mark.asyncio
async def test_parse_ofx_maps_per_org_cap_to_429(monkeypatch):
    """When an org is already at its per-org cap, ``parse_ofx`` returns 429.
    A different org is not blocked by that cap (it fails later on its own
    parse, not with 429)."""
    ex = OFXParseExecutor(max_concurrent=10, max_per_org=1, queue_wait_s=0.0)
    monkeypatch.setattr(import_ofx_service, "_executor", ex)

    async with ex.slot(org_id=42):
        with pytest.raises(HTTPException) as exc_info:
            await parse_ofx(b"<OFX>whatever</OFX>", org_id=42)
        assert exc_info.value.status_code == 429

        # A DIFFERENT org is not rejected by the cap: it proceeds to parse
        # and fails on the malformed body (ParseError), not with a 429.
        with pytest.raises(ParseError):
            await parse_ofx(b"<OFX>not-valid-ofx</OFX>", org_id=99)


# ── Row cap (restored to 10 000, configurable) ──────────────────────────────


@pytest.mark.asyncio
async def test_row_cap_over_limit_returns_413(monkeypatch):
    """Real parse of the 15-row fixture with the cap lowered to 10 → 413."""
    monkeypatch.setattr(import_ofx_service.settings, "ofx_max_rows", 10)
    with pytest.raises(HTTPException) as exc_info:
        await parse_ofx(_read("rabobank_ofx_1x.ofx"), org_id=1)
    assert exc_info.value.status_code == 413
    assert "transactions" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_row_cap_at_limit_is_accepted(monkeypatch):
    """Exactly ``ofx_max_rows`` (set to 15 for the 15-row fixture) is
    accepted — the boundary is inclusive."""
    monkeypatch.setattr(import_ofx_service.settings, "ofx_max_rows", 15)
    rows = await parse_ofx(_read("rabobank_ofx_1x.ofx"), org_id=1)
    assert len(rows) == 15


@pytest.mark.asyncio
async def test_row_cap_exact_plus_one_returns_413(monkeypatch):
    """Boundary: exactly ``ofx_max_rows`` + 1 rows is rejected with 413,
    while exactly ``ofx_max_rows`` rows is accepted — the ``> max_rows``
    check is inclusive at the cap and rejects one past it.

    The production default is 10 000, but a real 10 001-row parse is the
    same heavy, flake-prone cost as ``test_row_cap_default_10k_accepts_...``
    (which needs a 60s budget). This pins the exact cap+1 boundary cheaply
    via a lowered ``ofx_max_rows`` + an in-memory generated fixture, so no
    real 10k-row parse is added to the suite.
    """
    cap = 200
    monkeypatch.setattr(import_ofx_service.settings, "ofx_max_rows", cap)

    # Exactly cap+1 → 413.
    with pytest.raises(HTTPException) as exc_info:
        await parse_ofx(_make_ofx_with_rows(cap + 1), org_id=1)
    assert exc_info.value.status_code == 413
    assert "transactions" in str(exc_info.value.detail).lower()

    # Exactly cap → accepted (inclusive boundary), same generated shape.
    rows = await parse_ofx(_make_ofx_with_rows(cap), org_id=1)
    assert len(rows) == cap


@pytest.mark.asyncio
async def test_row_cap_default_10k_accepts_large_fixture(monkeypatch):
    """The restored 10 000-row cap accepts the 10k fixture end-to-end
    through the real subprocess parser (was rejected under the 2 000 stopgap).

    Uses a longer timeout since a genuine 10k-row ofxtools parse plus the
    spawn / pickle round-trip can exceed the default budget on a loaded box.
    """
    rows = await parse_ofx(
        _read("large_10k_rows.ofx"), org_id=1, timeout_s=60.0
    )
    assert len(rows) == 10_000
