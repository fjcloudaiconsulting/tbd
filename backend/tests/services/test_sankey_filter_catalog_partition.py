"""TBD-552 — the Sankey filter whitelist is a second hand-maintained inventory
of the transactions catalog, and nothing forced it to stay in sync with the
first. Two prior additions (TBD-507's ``currency``, TBD-471's ``transfer``)
each armed exactly this trap: a new catalog filter that ``_apply_user_filters``
never heard about.

⚠ The catching direction is ``catalog ⊆ SUPPORTED ∪ DENIED ∪ {TXN_TYPE}``,
asserted disjoint and exhaustive — NOT "whitelist ⊆ catalog", which passes
trivially the moment a new field is published and nobody remembers this file.

⚠ Enumerated from the RUNNING REGISTRY (``get_source("transactions").filters()``),
never the AST: CLAUDE.md records that an AST inventory fails open on every
shape it did not anticipate.

⚠ ``_SANKEY_DENIED_FILTER_FIELDS`` is read by NO production code — the raise
in ``_apply_user_filters`` already fires for anything outside SUPPORTED ∪
{TXN_TYPE}. A red partition here can therefore be "fixed" by filing a new
field under DENIED, which ships a 422 rather than a control. Only
``test_sankey_filter_strip_frontend_contract.py`` closes off that as a
permanent answer — the two fences are one chain, not belt-and-braces.
"""
from __future__ import annotations

from app.reports import sources as registry
from app.schemas.reports_query import FilterField
from app.services.sankey_service import (
    _SANKEY_DENIED_FILTER_FIELDS,
    _SANKEY_SUPPORTED_FILTER_FIELDS,
)

_TXN_TYPE_HANDLED_SEPARATELY = {FilterField.TXN_TYPE}


def test_sankey_filter_catalog_partition():
    src = registry.get_source("transactions")
    catalog = {FilterField(f.field) for f in src.filters()}

    accounted_for = (
        _SANKEY_SUPPORTED_FILTER_FIELDS
        | _SANKEY_DENIED_FILTER_FIELDS
        | _TXN_TYPE_HANDLED_SEPARATELY
    )

    missing = catalog - accounted_for
    assert not missing, (
        f"catalog field(s) {sorted(f.value for f in missing)} are published by "
        "the transactions source but neither SUPPORTED nor DENIED on the "
        "Sankey endpoint, nor TXN_TYPE — decide whether the Sankey editor gets "
        "a control for it or a documented 422"
    )

    # Disjointness: no field may be claimed by more than one bucket.
    overlap = (
        (_SANKEY_SUPPORTED_FILTER_FIELDS & _SANKEY_DENIED_FILTER_FIELDS)
        | (_SANKEY_SUPPORTED_FILTER_FIELDS & _TXN_TYPE_HANDLED_SEPARATELY)
        | (_SANKEY_DENIED_FILTER_FIELDS & _TXN_TYPE_HANDLED_SEPARATELY)
    )
    assert not overlap, f"field(s) claimed by more than one bucket: {overlap}"

    # SUPPORTED and DENIED must not name fields the catalog doesn't even
    # publish — a stale entry there is dead weight that this fence should
    # also catch, not just additions.
    stale = accounted_for - catalog
    assert not stale, f"bucket names field(s) absent from the live catalog: {stale}"
