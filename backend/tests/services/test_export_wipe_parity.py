"""Export-vs-erasure parity fence (TBD-222 §7, drains into TBD-223).

An org-scoped table that the export includes but neither ``wipe_org_data``
nor ``delete_org_cascade`` touches is a table whose personal data survives an
erasure request. Today that is a **28-table invisible gap**. This fence
converts it into an enumerated list that cannot grow silently: a new included
table must be either wiped or added to ``EXPORT_ONLY_ACKNOWLEDGED`` with a
TBD-223 note.

⚠ Deliberately NOT modelled on ``wipe_org_data``'s coverage — that is the
thing that failed (15 of 49, under a docstring promising the opposite).
"""
from __future__ import annotations

import pytest

from app.models.user import Organization
from app.services.admin_orgs_service import delete_org_cascade
from app.services.export_registry import EXPORT_DISPOSITION, Include, Via
from app.services.org_data_service import wipe_org_data

from tests.services.test_export_registry import session_factory  # noqa: F401


# ⚠ ``delete_org_cascade`` emits ``counts["settings"]`` for the table
# ``org_settings``. Filed as TBD-354; aliased here rather than worked around
# silently, so fixing TBD-354 makes this line dead code rather than a
# mysterious pass.
COUNTS_KEY_ALIASES = {"settings": "org_settings"}


# Included tables that erasure does NOT currently reach. Each entry is a
# TBD-223 work item, not an exemption. Shrinking this dict is TBD-223's DoD.
EXPORT_ONLY_ACKNOWLEDGED: dict[str, str] = {
    "audit_events": "TBD-223: audit rows are retained by design; erasure must pseudonymise, not delete",
    "ai_usage_ledger": "TBD-223: not wiped today",
    "dashboard_layouts": "TBD-223: not wiped today",
    "email_broadcast_recipients": "TBD-223: platform delivery log keyed by user_id, not wiped",
    "feedback_entries": "TBD-223: not wiped today",
    "notifications": "TBD-223: not wiped today (cascades via users on org delete only)",
    "org_ai_consents": "TBD-223: not wiped today",
    "org_ai_credentials": "TBD-223: not wiped today",
    "org_ai_default_caps": "TBD-223: not wiped today",
    "org_ai_default_routing": "TBD-223: not wiped today",
    "org_ai_feature_caps": "TBD-223: not wiped today",
    "org_ai_feature_routing": "TBD-223: not wiped today",
    "report_versions": "TBD-223: cascades via reports, no explicit count",
    "reports": "TBD-223: not wiped today",
    "scenarios": "TBD-223: not wiped today",
    "user_dismissed_announcements": "TBD-223: cascades via users, no explicit count",
    "user_notification_preferences": "TBD-223: cascades via users, no explicit count",
}


async def _erasure_keys(session_factory) -> set[str]:
    """Table names both erasure paths report having deleted, on a fresh org.

    Both functions already return ``dict[str, int]`` keyed by table name, so
    the coverage set is readable without parsing their source.
    """
    async with session_factory() as db:
        org = Organization(name="parity-probe", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        wiped = await wipe_org_data(db, org_id=org.id)
        cascaded = await delete_org_cascade(db, org_id=org.id)
        await db.commit()

    keys = set(wiped) | set(cascaded)
    return {COUNTS_KEY_ALIASES.get(k, k) for k in keys}


@pytest.mark.asyncio
async def test_every_included_table_is_wiped_or_explicitly_acknowledged(
    session_factory,
):
    covered = await _erasure_keys(session_factory)
    included = {t for t, d in EXPORT_DISPOSITION.items() if d.included}

    gap = sorted(included - covered - set(EXPORT_ONLY_ACKNOWLEDGED))
    assert not gap, (
        f"{len(gap)} table(s) are exported but neither wiped nor acknowledged: "
        f"{gap}. Either wire them into wipe_org_data or add an "
        f"EXPORT_ONLY_ACKNOWLEDGED entry carrying a TBD-223 note."
    )


@pytest.mark.asyncio
async def test_acknowledged_list_has_no_dead_entries(session_factory):
    """A table that erasure has since learned to wipe must leave the list.

    Without this, the TBD-223 backlog would never visibly shrink.
    """
    covered = await _erasure_keys(session_factory)
    included = {t for t, d in EXPORT_DISPOSITION.items() if d.included}

    now_covered = sorted(set(EXPORT_ONLY_ACKNOWLEDGED) & covered)
    assert not now_covered, (
        f"erasure now covers {now_covered}; remove from EXPORT_ONLY_ACKNOWLEDGED"
    )

    not_exported = sorted(set(EXPORT_ONLY_ACKNOWLEDGED) - included)
    assert not not_exported, (
        f"EXPORT_ONLY_ACKNOWLEDGED names non-included tables: {not_exported}"
    )


@pytest.mark.asyncio
async def test_org_settings_alias_is_still_needed(session_factory):
    """Pins TBD-354 so the alias cannot rot.

    When ``delete_org_cascade`` starts keying on ``org_settings``, this test
    fails and the alias should be deleted.
    """
    async with session_factory() as db:
        org = Organization(name="alias-probe", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        counts = await delete_org_cascade(db, org_id=org.id)
        await db.commit()

    assert "settings" in counts, "TBD-354 fixed? drop COUNTS_KEY_ALIASES"
    assert "org_settings" not in counts


def test_join_scoped_tables_name_a_parent_that_is_itself_included():
    """A ``Via`` whose parent is excluded would resolve to an unscoped, or
    unresolvable, predicate."""
    for table, disposition in EXPORT_DISPOSITION.items():
        if not isinstance(disposition, Include):
            continue
        scope = disposition.scope
        if isinstance(scope, Via):
            parent = EXPORT_DISPOSITION.get(scope.parent_table)
            assert parent is not None and parent.included, (
                f"{table} scopes via {scope.parent_table}, which is not an "
                f"included table"
            )
