"""TBD-404 verification probe. Deliberately failing backend test.

Exists only to prove DoD item 4: on a PR that DOES touch the backend, a real
shard failure must turn the `Backend Checks` required gate RED. If the gate's
`skipped` accept were reachable here, a broken suite would merge green.

This file is deleted before the PR merges.
"""


def test_tbd404_probe_deliberate_failure():
    assert False, "TBD-404 probe: this failure is intentional"
