"""AI spend accumulators cents -> micro-USD (TBD-630).

Revision ID: 091_micro_usd_metering
Revises: 090_platform_ai_models
Create Date: 2026-10-15

Renames and converts (x10,000) every accumulator of per-call AI cost:
``ai_usage_ledger.est_cost_cents`` -> ``cost_micros`` (INT -> BIGINT),
``platform_ai_spend.cents`` -> ``micros``, the ``platform_ai.cents`` plan meter
(counters, overrides, ``plans.usage_limits`` key) -> ``platform_ai.micros``.

Thresholds a human types (org caps, the global ceiling setting) stay in cents.
Migrations never import ``app``: the factor is the literal below.

The DML runs AFTER the last DDL: MySQL commits implicitly at each DDL, so the
InnoDB DML that follows commits together with the ``alembic_version`` stamp.

``downgrade`` is LOSSY by design: accumulators are CEILed (spend is never
under-stated), limits are FLOORed (a limit is never widened). It divides BEFORE
the DDL so the values fit INT again first.

Partial failure. MySQL commits implicitly before each DDL. What that leaves
behind depends on the direction:

- upgrade writes nothing before its first rename, and its DML commits together
  with the stamp after the last rename. A failure leaves UNCONVERTED values,
  ``alembic_version`` still ``090_platform_ai_models``, and possibly a renamed
  ledger and/or spend column. The init container retries and crash-loops on
  "Unknown column est_cost_cents". Recover: rename whatever was renamed back,
  with its 090 type, then let the rollout retry upgrade (the stamp is already
  right, so do not touch it). usage_counters, org_limit_overrides and
  plans.usage_limits need nothing: their DML rolled back. system_settings is
  never written.
    ALTER TABLE ai_usage_ledger CHANGE cost_micros est_cost_cents INT NOT NULL DEFAULT 0;
    ALTER TABLE platform_ai_spend CHANGE micros cents BIGINT NOT NULL DEFAULT 0;
  Confirm first: ``SUM`` of the ledger column equals the pre-check sum
  (unconverted). Never run a x10,000 UPDATE by hand.
- downgrade divides BEFORE its first rename, so the division commits at the
  start of that rename. A failure AT or after it leaves the data divided and
  rounded, ``alembic_version`` still ``091``, and columns not yet renamed back.
  Finish by hand: run whichever of the two CHANGE statements above has not
  happened yet, then ``alembic stamp 090_platform_ai_models``. Never re-run
  downgrade (it divides again) or upgrade (the sub-cent precision is gone).

Never ``alembic stamp 091`` to clear a crash-loop on "Unknown column
est_cost_cents": that leaves every value 10,000x too small and caps stop binding.

Pre-checks (prod, before merging): ``SELECT value FROM system_settings WHERE
`key` = 'platform_ai.global_monthly_cents'`` must be <= 900719925474. Take
T0 = ``UTC_TIMESTAMP()`` a few minutes in the past and record
``SUM(est_cost_cents) WHERE dispatched_at < T0`` (``dispatched_at`` is naive
UTC). Post-check: ``SUM(cost_micros) WHERE dispatched_at < T0`` equals the
recorded sum x 10000.
"""
from __future__ import annotations

import json
from typing import Callable, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import column, table


revision: str = "091_micro_usd_metering"
down_revision: Union[str, None] = "090_platform_ai_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FACTOR = 10_000
CENTS, MICROS = "platform_ai.cents", "platform_ai.micros"
# MeterLimit's ``le`` (feature_catalog): a larger converted limit would make
# every entitlement read raise.
MAX_LIMIT = 2**53 - 1


def _mul(x: int) -> int:
    return x * FACTOR


def _floor(x: int) -> int:
    return x // FACTOR


def convert_usage_limits(d: dict, src: str, dst: str, fn: Callable[[int], int]) -> dict | None:
    """``d`` with meter ``src`` renamed to ``dst`` and its limit through ``fn``;
    None when ``src`` is absent."""
    if src not in d:
        return None
    if dst in d:
        raise RuntimeError(f"usage_limits holds both {src!r} and {dst!r}")
    out = {k: v for k, v in d.items() if k != src}
    meter = dict(d[src])
    if meter.get("limit") is not None:
        meter["limit"] = fn(meter["limit"])
    out[dst] = meter
    return out


def _plan_rows(conn, src: str, dst: str, fn) -> list[tuple[int, dict]]:
    rows = []
    for pid, raw in conn.execute(sa.text("SELECT id, usage_limits FROM plans")).all():
        d = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        new = convert_usage_limits(d, src, dst, fn) if isinstance(d, dict) else None
        if new is not None:
            rows.append((pid, new))
    return rows


def down_plan_rows(conn) -> list[tuple[int, dict]]:
    return _plan_rows(conn, MICROS, CENTS, _floor)


def check_preconditions(conn) -> list[tuple[int, dict]]:
    """Refuse (before any write) when the conversion is unsafe; return the plan
    rows to rewrite."""
    n = conn.execute(sa.text(
        "SELECT COUNT(*) FROM org_ai_credentials WHERE platform_provider IS NOT NULL"
    )).scalar()
    if n:
        # A platform reservation could be in flight across the deploy: an old
        # pod would then settle a cent delta onto a micro counter.
        raise RuntimeError(f"{n} platform AI credential(s) exist; refusing to convert in-flight meters")
    rows = _plan_rows(conn, CENTS, MICROS, _mul)
    for _pid, d in rows:
        if (d[MICROS].get("limit") or 0) > MAX_LIMIT:
            raise RuntimeError("a plan platform_ai.cents limit x 10,000 exceeds 2**53-1")
    for (v,) in conn.execute(
        sa.text("SELECT limit_value FROM org_limit_overrides WHERE meter = :m"), {"m": CENTS}
    ):
        if v is not None and v * FACTOR > MAX_LIMIT:
            raise RuntimeError("an override platform_ai.cents limit x 10,000 exceeds 2**53-1")
    return rows


def convert_rows(conn, up: bool, plan_rows: list[tuple[int, dict]]) -> None:
    """Every data step, against the NEW column names (upgrade runs it after the
    renames, downgrade before renaming back)."""
    div = "DIV" if conn.dialect.name == "mysql" else "/"
    ceil = lambda c: f"({c} + {FACTOR - 1}) {div} {FACTOR}"  # noqa: E731
    floor = lambda c: f"{c} {div} {FACTOR}"  # noqa: E731
    acc = (lambda c: f"{c} * {FACTOR}") if up else ceil
    lim = (lambda c: f"{c} * {FACTOR}") if up else floor
    src, dst = (CENTS, MICROS) if up else (MICROS, CENTS)
    conn.execute(sa.text(f"UPDATE ai_usage_ledger SET cost_micros = {acc('cost_micros')}"))
    conn.execute(sa.text(f"UPDATE platform_ai_spend SET micros = {acc('micros')}"))
    conn.execute(
        sa.text(f"UPDATE usage_counters SET value = {acc('value')}, meter = :d WHERE meter = :s"),
        {"s": src, "d": dst},
    )
    conn.execute(
        sa.text(f"UPDATE org_limit_overrides SET limit_value = {lim('limit_value')}, meter = :d WHERE meter = :s"),
        {"s": src, "d": dst},
    )
    # Dicts, never json.dumps (that stores a string scalar; see 083).
    plans_t = table("plans", column("id"), column("usage_limits", sa.JSON))
    for pid, d in plan_rows:
        conn.execute(plans_t.update().where(plans_t.c.id == pid).values(usage_limits=d))


def upgrade() -> None:
    conn = op.get_bind()
    plan_rows = check_preconditions(conn)
    with op.batch_alter_table("ai_usage_ledger") as b:
        b.alter_column(
            "est_cost_cents", new_column_name="cost_micros", type_=sa.BigInteger(),
            existing_type=sa.Integer(), existing_nullable=False, existing_server_default="0",
        )
    with op.batch_alter_table("platform_ai_spend") as b:
        b.alter_column(
            "cents", new_column_name="micros", existing_type=sa.BigInteger(),
            existing_nullable=False, existing_server_default="0",
        )
    convert_rows(conn, True, plan_rows)


def downgrade() -> None:
    conn = op.get_bind()
    convert_rows(conn, False, down_plan_rows(conn))
    with op.batch_alter_table("ai_usage_ledger") as b:
        b.alter_column(
            "cost_micros", new_column_name="est_cost_cents", type_=sa.Integer(),
            existing_type=sa.BigInteger(), existing_nullable=False, existing_server_default="0",
        )
    with op.batch_alter_table("platform_ai_spend") as b:
        b.alter_column(
            "micros", new_column_name="cents", existing_type=sa.BigInteger(),
            existing_nullable=False, existing_server_default="0",
        )
