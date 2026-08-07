"""Format and truncation fences for the org export (TBD-222, spec §3).

⚠ **The whole design rests on one property: the trailer is LAST.** A
manifest-FIRST layout was rejected because a truncated file still parses and
still *looks* complete. The completeness record must be the thing truncation
destroys.

So these tests do the only thing that proves it: kill the generator
mid-table and assert the artifact has no terminal trailer and that the
verifier rejects it — then restore and assert the trailer is present with
counts matching the rows actually emitted.
"""
from __future__ import annotations

import json

import pytest

from app.services.export_registry import excluded_reasons, included_tables
from app.services.org_export_service import (
    SCHEMA_VERSION,
    ExportTooLarge,
    stream_org_export,
    verify_export,
)

from tests.services.test_export_registry import (  # noqa: F401  (fixtures)
    session_factory,
    two_orgs,
)


async def _lines(
    factory,
    org_id: int,
    *,
    stop_after: int | None = None,
    sink: list[bytes] | None = None,
    **kwargs,
):
    """Collect encoded lines, optionally abandoning the generator early.

    ``stop_after`` simulates the kill: the async generator is dropped
    mid-iteration exactly as it would be if the process died.

    ⚠ ``sink`` lets a caller see what was emitted BEFORE an exception. Without
    it, a test that only asserts ``pytest.raises`` cannot tell a pre-flight
    refusal from a refusal that shipped half the document first — and those
    are exactly the two implementations the bound is supposed to distinguish.
    """
    out: list[bytes] = sink if sink is not None else []
    async with factory() as db:
        async for line in stream_org_export(db, org_id=org_id, org_name="A", **kwargs):
            out.append(line)
            if stop_after is not None and len(out) >= stop_after:
                break
    return out


# ══ Shape ═════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_first_line_is_the_header(two_orgs):
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    header = json.loads(lines[0])
    assert header["record"] == "header"
    assert header["schema_version"] == SCHEMA_VERSION
    assert header["org_id"] == two_orgs["a"]["org_id"]
    assert set(header["expected_tables"]) == included_tables()
    assert header["excluded"] == excluded_reasons()


@pytest.mark.asyncio
async def test_last_line_is_the_trailer_and_counts_match_rows(two_orgs):
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])

    trailer = json.loads(lines[-1])
    assert trailer["record"] == "trailer"
    assert trailer["complete"] is True

    observed: dict[str, int] = {}
    for raw in lines[1:-1]:
        record = json.loads(raw)
        assert record["record"] == "row"
        observed[record["table"]] = observed.get(record["table"], 0) + 1

    assert {t: n for t, n in trailer["tables"].items() if n} == observed
    assert trailer["total_rows"] == sum(observed.values())


@pytest.mark.asyncio
async def test_trailer_hash_covers_every_preceding_byte(two_orgs):
    """Computed incrementally as lines ship; must equal a hash taken over the
    body after the fact."""
    import hashlib

    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    expected = hashlib.sha256(b"".join(lines[:-1])).hexdigest()
    assert json.loads(lines[-1])["sha256_body"] == expected


@pytest.mark.asyncio
async def test_a_complete_artifact_verifies(two_orgs):
    """⚠ ``total_rows`` is compared against rows counted HERE, from the body.

    The earlier form asserted ``result.total_rows == sum(result.tables
    .values())`` — but ``VerifyResult`` reads both of those off the same
    trailer, so it was a tautology that no implementation could fail.
    """
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    result = verify_export(lines)
    assert result.ok, result.reason

    independently_observed = sum(
        1 for raw in lines if json.loads(raw)["record"] == "row"
    )
    assert independently_observed > 0
    assert result.total_rows == independently_observed


# ══ Truncation — the property the whole format exists for ═════════════════


@pytest.mark.asyncio
async def test_a_truncated_artifact_has_no_trailer_and_is_rejected(two_orgs):
    """⚠ Kill the generator mid-table.

    The partial file is well-formed NDJSON all the way down — every line
    parses, the header still claims the full ``expected_tables`` — and
    betrays nothing except the missing trailer. That is exactly why the
    trailer must be last.
    """
    full = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    assert len(full) > 6, "need enough rows for a meaningful mid-stream kill"

    partial = await _lines(
        two_orgs["factory"], two_orgs["a"]["org_id"], stop_after=len(full) // 2
    )

    # Every line still parses; the header still looks authoritative.
    assert json.loads(partial[0])["record"] == "header"
    assert set(json.loads(partial[0])["expected_tables"]) == included_tables()
    for raw in partial:
        json.loads(raw)

    # ... and yet:
    assert not any(json.loads(raw)["record"] == "trailer" for raw in partial)

    result = verify_export(partial)
    assert result.ok is False
    assert "trailer" in (result.reason or "")


@pytest.mark.asyncio
async def test_verifier_rejects_a_forged_trailer_with_wrong_counts(two_orgs):
    """Counts are checked against rows actually observed, not trusted."""
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    trailer = json.loads(lines[-1])
    trailer["tables"]["transactions"] = trailer["tables"]["transactions"] + 7
    forged = lines[:-1] + [(json.dumps(trailer, sort_keys=True) + "\n").encode()]

    result = verify_export(forged)
    assert result.ok is False
    assert result.reason == "row counts disagree with trailer"


@pytest.mark.asyncio
async def test_verifier_rejects_a_body_edited_after_the_fact(two_orgs):
    """Dropping a row and fixing its count still breaks ``sha256_body``."""
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])

    victim = next(
        i for i, raw in enumerate(lines)
        if json.loads(raw)["record"] == "row"
        and json.loads(raw)["table"] == "transactions"
    )
    trailer = json.loads(lines[-1])
    trailer["tables"]["transactions"] -= 1
    trailer["total_rows"] -= 1
    tampered = (
        lines[:victim]
        + lines[victim + 1 : -1]
        + [(json.dumps(trailer, sort_keys=True) + "\n").encode()]
    )

    result = verify_export(tampered)
    assert result.ok is False
    assert result.reason == "sha256_body mismatch"


@pytest.mark.asyncio
async def test_verifier_rejects_an_artifact_missing_an_entire_table(two_orgs):
    """⚠ The manifest-first defect the trailer design exists to prevent.

    Drop every row of one table AND its trailer entry, exactly as a builder
    that skipped the table would produce. The per-table check cannot see it:
    the table is absent from the trailer and absent from the observed rows,
    so the two agree with each other. The header's ``expected_tables`` is the
    only independent record that the export promised the table at all —
    which is why ``verify_export`` must read it.
    """
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    header = json.loads(lines[0])
    victim = "scenarios"
    assert victim in header["expected_tables"]

    kept = [
        raw
        for raw in lines[:-1]
        if not (
            json.loads(raw)["record"] == "row" and json.loads(raw)["table"] == victim
        )
    ]
    assert len(kept) < len(lines) - 1, "sanity: the victim table had rows to drop"

    trailer = json.loads(lines[-1])
    dropped_rows = trailer["tables"].pop(victim)
    trailer["total_rows"] -= dropped_rows
    import hashlib

    trailer["sha256_body"] = hashlib.sha256(b"".join(kept)).hexdigest()
    forged = kept + [(json.dumps(trailer, sort_keys=True) + "\n").encode()]

    result = verify_export(forged)
    assert result.ok is False, (
        "an artifact missing a whole table certified as COMPLETE; the header "
        "promised it and the verifier never looked"
    )
    assert result.reason == "trailer tables do not match the header's expected_tables"


@pytest.mark.asyncio
async def test_verifier_rejects_a_malformed_trailer_without_crashing(two_orgs):
    """The trailer sits OUTSIDE ``sha256_body``, so it is untrusted input.

    A verifier that raises on a hand-edited trailer hands the operator a
    traceback where they needed a verdict.
    """
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    trailer = json.loads(lines[-1])
    trailer["total_rows"] = "not-a-number"
    forged = lines[:-1] + [(json.dumps(trailer, sort_keys=True) + "\n").encode()]

    result = verify_export(forged)
    assert result.ok is False
    assert result.reason == "malformed trailer"


@pytest.mark.asyncio
async def test_verifier_rejects_a_forged_total_rows(two_orgs):
    """⚠ ``sha256_body`` covers the body, NOT the trailer.

    Every number in the trailer is therefore unauthenticated. ``total_rows``
    has to be recomputed from the rows actually observed or a forged value
    verifies fine — and ``total_rows`` is what a recipient reads to decide
    whether they got everything.
    """
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    trailer = json.loads(lines[-1])
    trailer["total_rows"] = 99999
    forged = lines[:-1] + [(json.dumps(trailer, sort_keys=True) + "\n").encode()]

    result = verify_export(forged)
    assert result.ok is False
    assert result.reason == "total_rows disagrees with observed rows"


@pytest.mark.asyncio
async def test_verifier_rejects_content_after_the_trailer(two_orgs):
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    result = verify_export(lines + [lines[1]])
    assert result.ok is False
    assert result.reason == "content after the trailer"


# ══ Bounds ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_row_cap_refuses_before_emitting_anything(two_orgs):
    """Pre-flight ``COUNT(*)``: the refusal happens before the first byte.

    ⚠ ``pytest.raises`` alone does NOT pin this. An implementation that
    deleted ``preflight_counts`` and refused post-hoc — after streaming most
    of the document — raises the very same exception. The property that
    distinguishes them is that **nothing was emitted**, so that is what is
    asserted.
    """
    emitted: list[bytes] = []
    with pytest.raises(ExportTooLarge, match="exportable rows"):
        await _lines(
            two_orgs["factory"], two_orgs["a"]["org_id"], max_rows=1, sink=emitted
        )

    assert emitted == [], (
        f"the row cap emitted {len(emitted)} line(s) before refusing; a "
        f"pre-flight bound must refuse before the header ships"
    )


@pytest.mark.asyncio
async def test_byte_cap_aborts_mid_stream_and_leaves_no_trailer(two_orgs):
    """⚠ A row count does not bound bytes, and the check must be INCREMENTAL.

    The cap is derived from the real artifact rather than hardcoded. A
    hardcoded 600 sat *below the 1779-byte header*, so ``_emit`` raised on the
    header line, ``emitted`` was empty, and every assertion below was skipped
    — which left the incrementality itself unfenced: a variant checking the
    cap only once per drained table passed. Sizing the cap to "header + the
    first two rows, and not a byte more" makes the emitted-line count exact,
    so a coarser check overshoots and reddens.
    """
    full = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    cap = len(full[0]) + len(full[1]) + len(full[2])
    assert cap > len(full[0]), "sanity: the cap must sit ABOVE the header"

    emitted: list[bytes] = []
    with pytest.raises(ExportTooLarge, match="bytes"):
        async with two_orgs["factory"]() as db:
            async for line in stream_org_export(
                db, org_id=two_orgs["a"]["org_id"], org_name="A", max_bytes=cap
            ):
                emitted.append(line)

    # The header plus exactly two rows fit; the third crosses the cap and
    # aborts immediately. Anything more means the bound was not incremental.
    assert len(emitted) == 3, (
        f"expected the abort on the 3rd line, got {len(emitted)} line(s); "
        f"a cap checked per-table rather than per-line overshoots"
    )
    assert json.loads(emitted[0])["record"] == "header"
    assert all(json.loads(raw)["record"] == "row" for raw in emitted[1:])

    # ... and the abort landed long before the export was finished.
    seen = {json.loads(raw)["table"] for raw in emitted[1:]}
    last_table = sorted(included_tables())[-1]
    assert last_table not in seen, (
        f"the byte cap let the export reach its LAST table ({last_table}); "
        f"it did not abort mid-stream at all"
    )
    assert seen < included_tables()

    assert not any(json.loads(raw)["record"] == "trailer" for raw in emitted)
    assert verify_export(emitted).ok is False


@pytest.mark.asyncio
async def test_a_generous_byte_cap_does_not_fire(two_orgs):
    lines = await _lines(
        two_orgs["factory"], two_orgs["a"]["org_id"], max_bytes=100_000_000
    )
    assert verify_export(lines).ok


# ══ Encoding ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_money_is_encoded_as_a_string_not_a_float(two_orgs):
    """An export whose amounts have been through binary floating point is not
    a faithful copy of the ledger."""
    lines = await _lines(two_orgs["factory"], two_orgs["a"]["org_id"])
    row = next(
        json.loads(raw)
        for raw in lines
        if json.loads(raw)["record"] == "row"
        and json.loads(raw)["table"] == "transactions"
    )
    assert isinstance(row["data"]["amount"], str)
    assert row["data"]["amount"] == "10.00"
