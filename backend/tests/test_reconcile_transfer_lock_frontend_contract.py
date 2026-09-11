"""The inbox's transfer-leg button lock must match the server's refusals (TBD-385).

The server refuses FOUR targets on a reciprocally-linked transfer leg:
``matched`` (``_apply_match`` guard 2), ``edited`` (``_apply_edits``), and
``skipped`` / ``rejected`` (the TBD-385 guard in ``_reconcile_one``). The inbox
must not offer a button for any of them, per the standing rule that the client
never offers an action the server rejects.

⚠ WHY THIS FENCE EXISTS AT ALL. The two rosters are ASYMMETRIC. The server's
derives from ``transaction_filters.REVERTED_RECONCILIATION_STATES``, so adding a
third reverting state extends the refusal AUTOMATICALLY. The client's is a
hand-written literal that does not. Without this test, that day produces a
server that refuses and a UI that still offers -- which is the exact violation
TBD-385 was written to remove, re-created by the fix's own asymmetry.

⚠ THIS TEST FAILS RATHER THAN REGENERATING. If it is red the two sides
disagree: either a reverting state was added and the client was not updated, or
the client's list was edited. Read the diff and decide which side is wrong.
Auto-syncing would restore the drift it exists to stop.

⚠ Parsed, never grepped. A bare ``grep '"skipped"'`` over the file is satisfied
by the word appearing in the docstring above the constant -- and it does appear
there. This extracts the array literal specifically.

⚠ WHAT THIS DOES NOT COVER, stated so it is not over-cited. It proves the two
LISTS agree. It does not prove the client actually honours its own list (that is
`reconcile-page.test.tsx`'s two behavioural tests), nor that the server's
refusals are correct (that is F1/F5/F8 in
``tests/services/test_skipped_transfer_leg_balance.py``). All three are needed.

⚠⚠ AND ONLY HALF OF THIS COMPARISON IS DERIVED. ``REVERTED_RECONCILIATION_STATES``
is imported, so the skipped/rejected half tracks the server automatically. But
``PRE_TBD385_REFUSED`` is a hand-pinned literal HERE as well as in the client,
because ``matched`` and ``edited`` come from two separate guards with no shared
constant to import. So for those two the fence compares a literal against a
literal: **it cannot see ``_apply_match`` guard 2 or ``_apply_edits`` LOSING its
refusal.** Those are fenced behaviourally instead -- by
``test_apply_match_reciprocity_guards.py`` and by F42 in
``test_matched_row_actions.py`` -- and this test is not a substitute for either.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.transaction_filters import REVERTED_RECONCILIATION_STATES

# Refused on a reciprocal leg by guards that predate TBD-385 and are NOT part
# of the reverted roster. Pinned literally: they come from two separate guards,
# so there is no shared constant to derive them from.
PRE_TBD385_REFUSED = ("matched", "edited")


def _find_repo_root(start: Path) -> Path:
    """Walk up for the repo root instead of assuming a fixed depth.

    ⚠ NOT ``parents[2]``. Same walk and same reasoning as
    ``test_currency_list_frontend_contract.py``: a fixed depth silently breaks
    if this file moves one directory deeper, and the failure mode is a SKIP
    rather than a red.
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "deploy.yml").exists() and (
            candidate / ".do" / "app.yaml"
        ).exists():
            return candidate
    raise RuntimeError(
        "Could not locate repo root containing .github/workflows/deploy.yml "
        "and .do/app.yaml. Run these tests from a checked-out repo."
    )


def _lock_ts() -> str:
    """Read the frontend constant, in the container or on a plain checkout.

    ⚠⚠ THIS FAILS; IT NEVER SKIPS. A skipping guard is absent exactly when it
    matters and fails OPEN: rename the module and update only the frontend
    import, and a skipping version of this file would go green while the drift
    fence was permanently dead.

    ``frontend/lib`` is mounted read-only into the backend container
    (docker-compose.yml), which is why the constant lives in ``lib/`` rather
    than inline in ``ReconcileClient.tsx`` -- nothing mounts ``frontend/app``.
    """
    candidates = [
        Path("/app/frontend/lib/reconcile-transfer-lock.ts"),
        _find_repo_root(Path(__file__).resolve())
        / "frontend"
        / "lib"
        / "reconcile-transfer-lock.ts",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text()
    pytest.fail(
        "frontend/lib/reconcile-transfer-lock.ts is unreachable, so the drift "
        "fence between the inbox's button lock and the server's refusals did "
        f"not run. Looked in: {[str(c) for c in candidates]}. Inside the "
        "backend container this needs the ./frontend/lib mount; on a plain "
        "checkout it needs the repo root to be locatable."
    )


def _parsed_targets() -> list[str]:
    src = _lock_ts()
    m = re.search(
        r"export const TRANSFER_LOCKED_TARGETS\s*:\s*ReconciliationState\[\]\s*=\s*\[(.*?)\]",
        src,
        re.DOTALL,
    )
    assert m, (
        "Could not parse the TRANSFER_LOCKED_TARGETS array out of "
        "reconcile-transfer-lock.ts. If its declaration was reshaped, update "
        "this parser -- do NOT relax it into a substring search, which a "
        "docstring mention would satisfy."
    )
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_client_locks_every_state_the_server_refuses():
    """FENCE. Every server-refused target must appear in the client's lock list.

    KILLS: adding a third member to ``REVERTED_RECONCILIATION_STATES`` without
    updating the frontend. The server would refuse it automatically and the
    inbox would keep rendering the button.
    """
    parsed = _parsed_targets()
    expected = set(REVERTED_RECONCILIATION_STATES) | set(PRE_TBD385_REFUSED)
    missing = expected - set(parsed)
    assert not missing, (
        f"The inbox still offers {sorted(missing)} on a transfer leg, but the "
        "server refuses it. Add it to TRANSFER_LOCKED_TARGETS in "
        "frontend/lib/reconcile-transfer-lock.ts."
    )


def test_client_locks_nothing_the_server_permits():
    """FENCE, the other direction. The client must not hide a LEGAL action.

    KILLS: a defensive edit that adds ``accepted`` or ``pending_review`` to the
    lock list. Both are membership-neutral and both are PERMITTED on a
    reciprocal leg -- and ``accepted`` is the only legal exit from
    ``pending_review``. Hiding it strands the row, so ``batch.pending_count``
    never decrements and the batch can never close.
    """
    parsed = _parsed_targets()
    expected = set(REVERTED_RECONCILIATION_STATES) | set(PRE_TBD385_REFUSED)
    extra = set(parsed) - expected
    assert not extra, (
        f"TRANSFER_LOCKED_TARGETS hides {sorted(extra)}, which the server "
        "permits on a reciprocal transfer leg. Hiding 'accepted' in particular "
        "strands the row and prevents the batch from ever closing."
    )
