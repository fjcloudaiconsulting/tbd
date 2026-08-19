"""Shared registry for the full collected node-id set.

⚠ THIS MODULE EXISTS BECAUSE conftest.py IS LOADED TWICE, UNDER TWO NAMES.

Measured 2026-08-19 (TBD-421). pytest imports the conftest as the top-level
module `conftest`, and that is the copy whose `pytest_collection_modifyitems`
hook actually fires. A test doing `from tests.conftest import X` triggers a
SECOND import of the same file under the name `tests.conftest`, producing a
distinct module object with its own, never-updated globals:

    conftest:       4172 nodeids
    tests.conftest:    0 nodeids

Holding the set in a plain module that both sides import by the same dotted
name (`tests._durations_registry`) collapses that to one object. Do NOT move
this set back into conftest.py, and do NOT import it from `tests.conftest`.
"""

# Populated by conftest.pytest_collection_modifyitems, which runs `tryfirst`
# so it observes the whole suite before pytest-split deselects this shard's
# complement. Read by tests/test_test_durations_freshness.py.
COLLECTED_NODEIDS: set[str] = set()

# The same node ids in COLLECTION ORDER. `DurationBasedChunksAlgorithm` cuts the
# ordered item list into contiguous slices, so any simulation of the real
# partition needs the order, not just the membership.
COLLECTED_ORDER: list[str] = []
