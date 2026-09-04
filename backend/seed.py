"""Seed script — populate the system with realistic mock data for testing.

Run with: docker compose exec backend python seed.py
Or via: ./pfv seed

Generates 3 past months + current month of data relative to an anchor date,
which defaults to today.

Deterministic for a given anchor and RNG seed, on a FRESH database:

    SEED_ANCHOR_DATE=2026-03-17 SEED_RANDOM_SEED=42 ./pfv seed

⚠ Deterministic is not idempotent — re-running against an already-seeded org
APPENDS a second dataset. Run ``./pfv reset`` first. See the determinism
block below.
"""

import asyncio
import os
import random
from datetime import date, timedelta
from decimal import Decimal

import httpx
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from app.database import async_session

BASE = "http://localhost:8000"

USER = {
    "username": os.getenv("SEED_USERNAME", "demo"),
    "email": os.getenv("SEED_EMAIL", "demo@example.com"),
    "password": os.getenv("SEED_PASSWORD", "demo1234"),
    "first_name": os.getenv("SEED_FIRST_NAME", "Demo"),
    "last_name": os.getenv("SEED_LAST_NAME", "User"),
    "org_name": os.getenv("SEED_ORG", "Demo Household"),
}


# ── Determinism (TBD-345) ────────────────────────────────────────────────
#
# This script was documented as "a repeatable local dataset". Two
# things made that false: ``random`` was imported but never seeded, and
# every date derived from ``date.today()``, so the geometry changed with the
# day of the month. Both are now resolved ONCE at the top of ``main()`` and
# threaded through the pure planners below.
#
# "Repeatable" now means, precisely: **for a given anchor date and a given
# RNG seed, on a FRESH database, the dataset is identical.**
#
# ⚠ It does NOT mean idempotent. ``POST /api/v1/accounts`` has no
# duplicate-name check, so re-running against an already-seeded org APPENDS
# a second set of accounts and a second set of transactions. Run
# ``./pfv reset`` first if you want the dataset this script describes.
# Making the seed idempotent needs a product ruling (append / replace /
# refuse-when-dirty, the last being what ``demo_seed_service`` already
# answers with 409 ``org_has_data``) and is tracked separately.

DEFAULT_RANDOM_SEED = 20260101

MONTHLY_FIXED = [
    {"acct": "ING Checking", "cat": "rent_mortgage", "desc": "Rent - Apartment", "amount": "1200.00", "day": 1},
    {"acct": "ING Checking", "cat": "electricity", "desc": "Vattenfall Electricity", "amount": "85.00", "day": 3},
    {"acct": "ING Checking", "cat": "water", "desc": "Water Board", "amount": "35.00", "day": 3},
    {"acct": "ING Checking", "cat": "internet", "desc": "KPN Internet", "amount": "49.99", "day": 5},
    {"acct": "ING Checking", "cat": "phone", "desc": "T-Mobile Plan", "amount": "29.99", "day": 5},
    {"acct": "ING Checking", "cat": "health_insurance", "desc": "Zilveren Kruis", "amount": "135.00", "day": 1},
    {"acct": "ING Checking", "cat": "gym", "desc": "BasicFit Membership", "amount": "29.99", "day": 1},
    {"acct": "ING Checking", "cat": "streaming", "desc": "Netflix", "amount": "17.99", "day": 10},
    {"acct": "ING Checking", "cat": "streaming", "desc": "Spotify Family", "amount": "16.99", "day": 10},
    {"acct": "ING Checking", "cat": "auto_insurance", "desc": "ANWB Car Insurance", "amount": "78.00", "day": 15},
]

VARIABLE = [
    {"acct": "Amex Platinum", "cat": "groceries", "desc": "Albert Heijn", "min": 40, "max": 120},
    {"acct": "Amex Platinum", "cat": "groceries", "desc": "Jumbo Supermarket", "min": 30, "max": 80},
    {"acct": "Amex Platinum", "cat": "restaurants", "desc": "Restaurant dinner", "min": 35, "max": 90},
    {"acct": "Amex Platinum", "cat": "coffee_shops", "desc": "Coffee & pastry", "min": 5, "max": 15},
    {"acct": "Amex Platinum", "cat": "fast_food", "desc": "Thuisbezorgd delivery", "min": 15, "max": 40},
    {"acct": "Amex Platinum", "cat": "fuel", "desc": "Shell fuel", "min": 50, "max": 90},
    {"acct": "Amex Platinum", "cat": "clothing", "desc": "H&M / Zara", "min": 30, "max": 120},
    {"acct": "ING Checking", "cat": "parking_tolls", "desc": "Parking garage", "min": 5, "max": 20},
    {"acct": "Revolut", "cat": "entertainment", "desc": "Cinema tickets", "min": 15, "max": 30},
    {"acct": "Revolut", "cat": "books_media", "desc": "Amazon Kindle", "min": 8, "max": 25},
]


def resolve_anchor(raw: str | None = None) -> date:
    """The date the whole dataset is generated relative to.

    Defaults to ``date.today()`` — deliberately, and against the letter of
    TBD-345, which asked for a fixed default anchor. A pinned past default
    would rot: ``billing_service.ensure_future_periods`` anchors its stubs to
    the OPEN period's ``start_date``, so a permanently-past anchor hands every
    developer an org whose open period is months behind the calendar. It would
    also make the ``is_current`` branch in ``plan_transactions`` structurally
    dead — no generated month would ever be the real current month, so the
    credit-card ``pending`` state would never be demoed at all. The knob exists
    for reproducibility; the default exists so that ``./pfv seed`` gives the
    person running it a working app.

    ⚠ A malformed value RAISES rather than falling back to today. A silent
    fallback would be the same defect this function exists to remove: a caller
    that believes it pinned an anchor while actually running on the wall clock.
    """
    raw = os.getenv("SEED_ANCHOR_DATE") if raw is None else raw
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"SEED_ANCHOR_DATE must be an ISO-8601 date (YYYY-MM-DD); got {raw!r}"
        ) from exc


def resolve_rng(raw: str | None = None) -> random.Random:
    """The RNG every random value in the dataset is drawn from.

    Returns a dedicated ``random.Random`` INSTANCE rather than calling
    ``random.seed()``. A module-global reseed is action-at-a-distance for
    anything that imports this module (the seed tests do), and it makes the
    "same seed, same dataset" property depend on nothing else having drawn
    from the global stream in between — the kind of coupling that looks
    deterministic in a single-run smoke test and is not.
    """
    raw = os.getenv("SEED_RANDOM_SEED") if raw is None else raw
    if raw is None or raw == "":
        return random.Random(DEFAULT_RANDOM_SEED)
    try:
        return random.Random(int(raw))
    except ValueError as exc:
        raise ValueError(
            f"SEED_RANDOM_SEED must be an integer; got {raw!r}"
        ) from exc


def plan_billing_periods(anchor: date) -> tuple[list[tuple[date, date]], date]:
    """The closed periods to POST, plus the start of the open one. Pure.

    Every boundary is ``first_of_month ± months + N days`` with N ≤ 24, so the
    three windows are a function of the anchor's MONTH only and do not move
    within a month. The one within-month variable is whether the third
    definition's end (the 24th of the anchor's month) has passed, which flips
    on day 25 and yields two stable regimes:

    * anchor day 1-24  → **2** closed periods; open period starts on the 24th
      of the PREVIOUS month;
    * anchor day 25-31 → **3** closed periods; open period starts on the 25th
      of the anchor's own month.

    Both regimes are contiguous, gap-free, overlap-free, and satisfy
    ``current_start <= anchor`` (swept across 180 anchors spanning a 28-day
    February, a leap February, and 30- and 31-day months). That regime split
    is a property to pin, not a bug to fix: once the anchor is injectable both
    regimes are fully deterministic, which is all this script promises.
    """
    first = anchor.replace(day=1)
    period_defs = [
        # (start, end) — salary on 25th, 23rd, 24th
        (first - relativedelta(months=3) + timedelta(days=24),
         first - relativedelta(months=2) + timedelta(days=21)),  # 25th → 22nd next
        (first - relativedelta(months=2) + timedelta(days=22),
         first - relativedelta(months=1) + timedelta(days=22)),  # 23rd → 23rd next
        (first - relativedelta(months=1) + timedelta(days=23),
         first + timedelta(days=23)),  # 24th → 24th next (closed)
    ]
    closed = [(start, end) for start, end in period_defs if end < anchor]
    # The open period starts the day after the last CLOSED end, read straight
    # off `closed` rather than re-deriving `end < anchor` a second time. The
    # original code evaluated that predicate twice, in two separate
    # expressions, which could drift apart: relax one to `<=` and the open
    # period lands exactly ON the last closed end, overlapping it.
    #
    # `closed` is never empty, so `[-1]` is safe: `period_defs[1][1]` is the
    # 23rd of the anchor's previous month, which is strictly before any anchor
    # in its own month, so at least two definitions always qualify.
    last_end = closed[-1][1]
    return closed, last_end + timedelta(days=1)


def plan_transactions(anchor: date, rng: random.Random) -> list[dict]:
    """Every transaction to POST, keyed by account NAME and category SLUG.

    Pure: no network, no server-assigned ids. Ids do not exist at plan time,
    so the plan is expressed in names and slugs and ``main()`` resolves them
    when posting. That is what lets the dataset be swept across many anchors
    in-process, in milliseconds, rather than by running the seed against a
    live backend once per anchor.

    ⚠ Every RNG draw is UNCONDITIONAL — made before any date filter, never
    inside an ``if tx_date <= anchor`` branch. Drawing inside the branch would
    make the stream POSITION depend on which dates happen to pass the filter,
    so two runs at different anchors would diverge in ways that have nothing to
    do with the dates.

    ⚠ That buys anchor-independence for the three PAST months only, not for the
    anchor's own month, and the difference is worth stating precisely because it
    is easy to over-claim. ``day_cap`` below is ``anchor.day`` in the current
    month, and ``Random.randint`` rejection-samples: the number of Mersenne
    Twister words it consumes is a function of its BOUND. So the current month's
    draws do shift the stream by day-of-month. It is harmless here only because
    ``offset == 0`` is the LAST loop iteration, so nothing downstream of it
    depends on the stream position — the past months are already planned. The
    cap is kept deliberately: it is what makes the current month fill in
    progressively rather than sparsely, which is most of this dataset's demo
    value. Determinism at a FIXED anchor is unaffected either way.
    """
    plan: list[dict] = []

    for offset in range(3, -1, -1):
        m_start = anchor.replace(day=1) - relativedelta(months=offset)
        is_current = offset == 0

        salary_day = rng.choice([23, 24, 25])
        want_side_income = rng.random() > 0.5
        side_income_day = rng.randint(10, 20)
        side_income_amount = rng.randint(200, 800)
        num_var = rng.randint(10, 18)
        # Cap at the anchor's day in the current month so it fills in
        # progressively, and at 28 always so `replace(day=...)` cannot raise in
        # February.
        day_cap = min(anchor.day if is_current else 28, 28)
        var_draws = []
        for _ in range(num_var):
            exp = rng.choice(VARIABLE)
            var_draws.append(
                (exp, rng.randint(1, day_cap), round(rng.uniform(exp["min"], exp["max"]), 2))
            )

        # Salary (23rd-25th, only if the date has passed)
        sal_date = m_start.replace(day=min(salary_day, 28))
        if sal_date <= anchor:
            plan.append({
                "kind": "transaction", "acct": "ING Checking", "cat": "paycheck",
                "desc": "W&B Monthly Salary", "amount": "6500.00",
                "type": "income", "status": "settled", "date": sal_date,
            })

        # Side income (occasional)
        if want_side_income:
            si_date = m_start.replace(day=side_income_day)
            if si_date <= anchor:
                plan.append({
                    "kind": "transaction", "acct": "Revolut", "cat": "side_hustles",
                    "desc": "Freelance consulting", "amount": str(side_income_amount),
                    "type": "income", "status": "settled", "date": si_date,
                })

        # Fixed expenses
        for exp in MONTHLY_FIXED:
            tx_date = m_start.replace(day=min(exp["day"], 28))
            if tx_date <= anchor:
                plan.append({
                    "kind": "transaction", "acct": exp["acct"], "cat": exp["cat"],
                    "desc": exp["desc"], "amount": exp["amount"],
                    "type": "expense", "status": "settled", "date": tx_date,
                })

        # Variable expenses (10-18 per month, spread across the month)
        for exp, day, amount in var_draws:
            tx_date = m_start.replace(day=day)
            if tx_date <= anchor:
                plan.append({
                    "kind": "transaction", "acct": exp["acct"], "cat": exp["cat"],
                    "desc": exp["desc"], "amount": str(amount),
                    "type": "expense",
                    # Current month credit card = pending
                    "status": "pending" if exp["acct"] == "Amex Platinum" and is_current else "settled",
                    "date": tx_date,
                })

        # Monthly savings transfer (26th)
        #
        # ⚠ Category is `transfer`, NOT `general_savings` (TBD-345).
        # `transaction_service` requires a transfer's category to be
        # `CategoryType.BOTH`, and the seeded catalog has exactly two such
        # categories: `transfer` and `credit_card_payment`. `general_savings`
        # and `investments` are expense-only, so both transfer POSTs below
        # answered 400 "Transfer category must accept both income and expense"
        # and produced NOTHING — silently, because the seed checks no status
        # on transaction posts. The dataset has had zero transfers for as long
        # as that rule has existed, while the summary line counted them.
        xfer_date = m_start.replace(day=26)
        if xfer_date <= anchor:
            plan.append({
                "kind": "transfer", "from_acct": "ING Checking", "to_acct": "ING Savings",
                "cat": "transfer", "desc": "Monthly savings",
                "amount": "500.00", "status": "settled", "date": xfer_date,
            })

        # Investment contribution (15th, bi-monthly)
        if offset % 2 == 0:
            inv_date = m_start.replace(day=15)
            if inv_date <= anchor:
                plan.append({
                    "kind": "transfer", "from_acct": "ING Checking", "to_acct": "Degiro",
                    "cat": "transfer", "desc": "ETF investment",
                    "amount": "300.00", "status": "settled", "date": inv_date,
                })

    return plan


def _raise_loudly(r: httpx.Response, item: dict) -> None:
    """Raise on a non-2xx, printing the server's message first.

    ``httpx.HTTPStatusError`` carries the status but not the response body, and
    for a seed script the 4xx detail IS the diagnostic — "Transfer category must
    accept both income and expense" is the difference between a five-second fix
    and an afternoon. Kept as a helper so the two post sites cannot drift.
    """
    if r.is_success:
        return
    print(f"   FAILED {item.get('kind')} {item.get('desc')!r} "
          f"on {item.get('date')}: {r.status_code} {r.text}")
    r.raise_for_status()


def billing_period_outcome(r: httpx.Response) -> str:
    """Interpret a ``POST /api/v1/settings/billing-period`` response.

    ``./pfv seed`` is documented as a repeatable dataset, and the period dates below are deterministic for a given
    ``anchor``, so a re-run at the same anchor posts start dates that
    already exist.
    TBD-232 gave that endpoint a duplicate-start pre-flight that answers
    409 ``billing_period_exists``; treat that one status as "already
    seeded" and carry on, exactly like the login-instead-of-register and
    ``if r.status_code == 201`` account guards earlier in this script.

    TBD-239 added a second conflict code, ``billing_period_overlap``, and it
    has to be absorbed too.

    ⚠ The mechanism, measured against the real endpoint (TBD-345), because
    the obvious reading of it is wrong in BOTH directions. The endpoint runs
    two gates in order: an exact-start pre-flight answering 409
    ``billing_period_exists``, then a containment check answering 409
    ``billing_period_overlap``. ``plan_billing_periods`` derives every
    boundary from the anchor's MONTH, so the windows do not move within a
    month at all. Therefore:

    * re-run at the SAME anchor → every start is identical, the pre-flight
      fires first, and all POSTs answer ``exists``. It is NOT true that "a
      re-run on a later day shifts the window";
    * re-run at a different anchor within the same month → the three CLOSED
      POSTs still answer ``exists`` (the windows are a function of the month,
      and the third collides with the previous run's OPEN row's start), but if
      the two anchors straddle the day-24/25 regime boundary the open POST
      computes a later start, misses both gates, and answers ``created``. So
      "same month" is NOT uniformly safe — see the ⚠ below;
    * anchor moved 1-2 months → the windows shift far enough to land across
      the old rows but near enough to still intersect them → ``overlaps``;
    * anchor moved 3+ months → the new windows miss the old ones entirely,
      so there is no 409 at all and every POST answers ``created``.

    Raising on ``overlaps`` would abort ``./pfv seed`` at step 5, before
    recurring, budgets, forecast plans and reports — which is why it is
    absorbed unconditionally rather than being made conditional on whether
    the anchor was pinned. A pinned anchor does make the overlap branch
    unreachable for benign reasons, but conditioning on that would be an
    alarm on the wrong door: in two of the three changed-anchor shapes above
    there is no 409 to condition on at all.

    ⚠ What none of this absorbs, and what a re-seed actually costs: whenever a
    re-run's open-period start moves later than the existing open row's start,
    that POST misses both gates — the containment check's open arm only rejects
    an existing open row whose ``start_date >= body.start_date`` — so no
    conflict fires and a SECOND open row is created, reported here as
    ``created``. That is the shape ``billing_service`` calls ``duplicate_open``
    and warns about at ``get_current_period``. The fix is a clean database
    (``./pfv reset``) before re-seeding, not a more tolerant helper here.

    Every OTHER non-2xx still raises. That is the contract-drift guard the
    endpoint needs: TBD-232 also moved it from query params to a Pydantic
    body, and a swallowed 422 would leave the demo org with zero billing
    periods under a cheerful "Seed complete!".
    """
    if r.status_code == 409:
        try:
            code = r.json().get("code")
        except ValueError:
            code = None
        if code == "billing_period_exists":
            return "exists"
        if code == "billing_period_overlap":
            return "overlaps"
    r.raise_for_status()
    return "created"


async def ensure_verified(username: str) -> None:
    """Mark the seed user's email verified, so ``/auth/login`` will accept it.

    ``POST /auth/login`` 403s any user with ``email_verified = 0`` and there
    is no mailbox in a dev stack to click the link in. TBD-344 makes
    ``/register`` verify the FIRST user on an empty ``users`` table, which
    covers the common case, but not the two this script still hits:

    * A machine that has already run ``./pfv seed`` once, from before that
      fix, is WEDGED — the demo user exists and is unverified, so login 403s,
      register 409s, and the script dies at its ``!= 201`` guard.
    * ``SEED_USERNAME=alice`` (CONTRIBUTING, "Seeding mock data") registers a
      SECOND user, where ``user_count > 0`` and no bypass applies.

    ⚠ Direct DB write, deliberately, and NOT an HTTP token redemption: on a
    wedged DB no HTTP response yields a ``user_id`` to mint a token for (the
    login 403 detail carries none, the register 409 detail is a bare string,
    and ``resend-verification-public`` is generic on purpose to prevent
    account enumeration), so the token path would have to open a DB session
    anyway and would only add a second thing to keep in sync.

    ⚠ This is a PRECONDITION, not data. Everything this script seeds still
    goes through the API — see ``billing_period_outcome`` above and the
    contract-drift guard its docstring describes. Do not read this as
    precedent for writing seed DATA out of band.

    No-ops when the username does not exist: the UPDATE simply matches zero
    rows, which is the correct behaviour on the cold-start path where the
    account has not been registered yet.
    """
    async with async_session() as db:
        result = await db.execute(
            text("UPDATE users SET email_verified = 1 WHERE username = :u"),
            {"u": username},
        )
        # Read rowcount before the commit purely so it cannot depend on cursor
        # lifetime. Reading it afterwards is in fact safe for a
        # single-parameter-set UPDATE — `CursorResult.rowcount` is a
        # memoized_property and SQLAlchemy 2.0 transfers `cursor.rowcount` into
        # the execution context BEFORE closing the cursor for exactly this
        # statement shape (the `preserve_rowcount` execution option is what
        # extends that to INSERT/SELECT/executemany, which this is not).
        # Measured on MySQL 8 / aiomysql: 1 before commit, still 1 after the
        # session closed AND after another session had churned the pool with a
        # 3-row UPDATE. Ordering it this way just removes the need to know that
        # rule to read the code.
        changed = result.rowcount
        await db.commit()
    # `changed` is the MATCHED count, not the modified count: SQLAlchemy's MySQL
    # dialect connects with CLIENT_FOUND_ROWS, so a no-op rewrite of a row that
    # is already 1 still reports 1 — measured, and identical to SQLite. So this
    # line prints whenever the account exists and stays quiet only when the
    # username matched nothing, on both backends.
    if changed:
        print(f"   Marked {username}'s email verified")


async def main():
    # Resolved ONCE, before anything is posted, so the whole run shares one
    # anchor and one RNG stream. Both raise on a malformed value rather than
    # falling back, so a caller can never believe it pinned one and be wrong.
    anchor = resolve_anchor()
    rng = resolve_rng()

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        print("=== PFV2 Seed Script ===\n")
        # Print BOTH knobs: reproducing a run needs the seed as well as the
        # anchor, and the default seed is otherwise only discoverable by
        # reading DEFAULT_RANDOM_SEED.
        print(f"Anchor date: {anchor.isoformat()}"
              f"{'' if os.getenv('SEED_ANCHOR_DATE') else ' (today; set SEED_ANCHOR_DATE to pin)'}")
        print(f"RNG seed:    {os.getenv('SEED_RANDOM_SEED') or DEFAULT_RANDOM_SEED}"
              f"{'' if os.getenv('SEED_RANDOM_SEED') else ' (default)'}\n")

        # Auth
        print("1. Authenticating...")
        # TBD-344, call 1 of 2 — MUST precede the first login. A machine that
        # already ran ./pfv seed before the register fix has the demo user
        # sitting unverified: login 403s, the register below 409s, and the
        # `!= 201` guard returns before any data is seeded. Anything placed
        # after that return is dead code on exactly the machines that need it.
        await ensure_verified(USER["username"])
        r = await c.post("/api/v1/auth/login", json={"login": USER["username"], "password": USER["password"]})
        if r.status_code != 200:
            print("   User not found, registering...")
            r = await c.post("/api/v1/auth/register", json={
                "username": USER["username"],
                "email": USER["email"],
                "password": USER["password"],
                "first_name": USER["first_name"],
                "last_name": USER["last_name"],
                "org_name": USER["org_name"],
            })
            if r.status_code != 201:
                print(f"   Registration failed: {r.text}")
                return
            # TBD-344, call 2 of 2 — the SEED_USERNAME=alice case. That is a
            # second user, so `user_count > 0` and /register's bootstrap
            # verification does not apply. Unconditional: on the cold-start
            # path the row is already verified and this is a no-op write.
            await ensure_verified(USER["username"])
            r = await c.post("/api/v1/auth/login", json={"login": USER["username"], "password": USER["password"]})

        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"   Logged in as {USER['username']}")

        # Account types
        r = await c.get("/api/v1/account-types", headers=headers)
        account_types = {at["slug"]: at["id"] for at in r.json() if at["slug"]}

        # Create accounts
        print("\n2. Creating accounts...")
        accounts = {}
        # ⚠ `opening_balance`, NOT `balance` (TBD-345). `AccountCreate` has no
        # `balance` field and no `extra="forbid"`, so the old `"balance"` key
        # was silently DROPPED by pydantic and `opening_balance` fell back to
        # its `Decimal("0.00")` default: every seeded account was created at
        # zero while the line below cheerfully printed "ING Checking (5000.00
        # EUR)". The L1.1 pentest follow-up removed the free-form `balance`
        # create input (it seeded `Account.balance` with no transaction backing
        # and no audit row) and `opening_balance` is now the sole entry point;
        # the router sets `Account.balance` from it.
        acct_defs = [
            {"name": "ING Checking", "type": "checking", "opening_balance": "5000.00", "currency": "EUR"},
            {"name": "ING Savings", "type": "savings", "opening_balance": "12000.00", "currency": "EUR"},
            {"name": "Amex Platinum", "type": "credit_card", "opening_balance": "0.00", "currency": "EUR", "close_day": 15},
            {"name": "Revolut", "type": "checking", "opening_balance": "800.00", "currency": "EUR"},
            {"name": "Degiro", "type": "investment", "opening_balance": "25000.00", "currency": "EUR"},
        ]
        # ⚠ `opening_balance_date` must be sent, and must derive from the anchor.
        # `Account.opening_balance_date` is `server_default=func.current_date()`
        # and the router omits it from the insert when the caller omits it, so it
        # lands on the RUN date, not the anchor. That breaks determinism outright
        # (the same pinned command on two days writes two different datasets) and
        # it silently wrecks the seeded Net Worth report: `reports/sources/
        # networth.py` buckets the opening-balance stream by this column, so with
        # a pinned past anchor every bucket in the generated window omits the
        # opening balances and the curve steps by the whole ~42.8k in whichever
        # month the seed happened to run.
        #
        # This was INERT before the `opening_balance` fix above — the dropped
        # `balance` key meant every account opened at 0.00, so the date carried
        # nothing. Correcting one half of this payload is what arms the other.
        # The start of the earliest generated month is the semantically right
        # value: it must precede the first transaction.
        opening_date = (anchor.replace(day=1) - relativedelta(months=3)).isoformat()
        for ad in acct_defs:
            r = await c.post("/api/v1/accounts", headers=headers, json={
                "name": ad["name"], "account_type_id": account_types[ad["type"]],
                "opening_balance": ad["opening_balance"], "currency": ad["currency"],
                "opening_balance_date": opening_date,
                "close_day": ad.get("close_day"),
            })
            # Loud, like the transaction posts below. An ignored response here is
            # precisely how the dropped `balance` key survived for as long as
            # `AccountCreate` has existed; a non-201 would otherwise surface much
            # later as a bare `KeyError: 'ING Checking'` with the server's actual
            # message discarded.
            if r.status_code != 201:
                print(f"   FAILED {ad['name']}: {r.status_code} {r.text}")
                r.raise_for_status()
            accounts[ad["name"]] = r.json()["id"]
            print(f"   {ad['name']} ({ad['opening_balance']} {ad['currency']})")

        if "ING Checking" in accounts:
            await c.put(f"/api/v1/accounts/{accounts['ING Checking']}", headers=headers, json={"is_default": True})

        # Categories
        r = await c.get("/api/v1/categories", headers=headers)
        cats = {cat["slug"]: cat["id"] for cat in r.json() if cat["slug"]}
        master_cats = {cat["slug"]: cat["id"] for cat in r.json() if cat["parent_id"] is None and cat["slug"]}
        print(f"\n3. {len(cats)} categories loaded")

        # --- Transactions: 3 past months + current month ---
        print("\n4. Creating transactions...")
        tx_count = 0

        # The dataset is planned in full, purely, BEFORE anything is posted.
        # `plan_transactions` yields account names and category slugs; the id
        # resolution below is the only part that needs the server. Entries
        # naming a category this install does not have are skipped, which is
        # what the old inline `exp["cat"] in cats` guards did.
        for item in plan_transactions(anchor, rng):
            if item["cat"] not in cats:
                continue
            if item["kind"] == "transfer":
                r = await c.post("/api/v1/transactions/transfer", headers=headers, json={
                    "from_account_id": accounts[item["from_acct"]],
                    "to_account_id": accounts[item["to_acct"]],
                    "category_id": cats[item["cat"]],
                    "description": item["desc"], "amount": item["amount"],
                    "status": item["status"], "date": item["date"].isoformat(),
                })
                # Loud, deliberately. These posts previously checked nothing, so
                # every transfer 400'd invisibly for as long as the BOTH-category
                # rule has existed while `tx_count` counted them anyway — the
                # summary line claimed 117 transactions over 107 real rows.
                # Print the body first: HTTPStatusError carries only the status,
                # and for a seed script the 4xx detail is the whole diagnostic.
                _raise_loudly(r, item)
                tx_count += 2
            else:
                r = await c.post("/api/v1/transactions", headers=headers, json={
                    "account_id": accounts[item["acct"]], "category_id": cats[item["cat"]],
                    "description": item["desc"], "amount": item["amount"],
                    "type": item["type"], "status": item["status"],
                    "date": item["date"].isoformat(),
                })
                _raise_loudly(r, item)
                tx_count += 1

        print(f"   Created {tx_count} transactions")

        # Create explicit billing periods with varying salary days
        print("\n5. Creating billing periods...")
        closed_periods, current_start = plan_billing_periods(anchor)
        for start, end in closed_periods:
            r = await c.post("/api/v1/settings/billing-period", headers=headers,
                             json={"start_date": start.isoformat(), "end_date": end.isoformat()})
            outcome = billing_period_outcome(r)
            print(f"   Period: {start} — {end} ({outcome})")

        # Current open period (starts day after last closed)
        r = await c.post("/api/v1/settings/billing-period", headers=headers,
                         json={"start_date": current_start.isoformat()})
        outcome = billing_period_outcome(r)
        print(f"   Current period: {current_start} — open ({outcome})")

        await c.put("/api/v1/settings/billing-cycle", headers=headers,
                    json={"billing_cycle_day": 25})
        print("   Default cycle day set to 25")

        # Recurring
        print("\n6. Creating recurring transactions...")
        rec_defs = [
            {"acct": "ING Checking", "cat": "rent_mortgage", "desc": "Rent - Apartment", "amount": "1200.00", "freq": "monthly", "day": 1, "auto": True},
            {"acct": "ING Checking", "cat": "gym", "desc": "BasicFit Membership", "amount": "29.99", "freq": "monthly", "day": 1, "auto": True},
            {"acct": "ING Checking", "cat": "streaming", "desc": "Netflix", "amount": "17.99", "freq": "monthly", "day": 10, "auto": True},
            {"acct": "ING Checking", "cat": "streaming", "desc": "Spotify Family", "amount": "16.99", "freq": "monthly", "day": 10, "auto": True},
            {"acct": "ING Checking", "cat": "health_insurance", "desc": "Zilveren Kruis", "amount": "135.00", "freq": "monthly", "day": 1, "auto": True},
        ]
        next_month = anchor.replace(day=1) + relativedelta(months=1)
        for rd in rec_defs:
            if rd["cat"] in cats:
                await c.post("/api/v1/recurring", headers=headers, json={
                    "account_id": accounts[rd["acct"]], "category_id": cats[rd["cat"]],
                    "description": rd["desc"], "amount": rd["amount"], "type": "expense",
                    "frequency": rd["freq"], "next_due_date": next_month.replace(day=min(rd["day"], 28)).isoformat(),
                    "auto_settle": rd["auto"],
                })
        print(f"   Created {len(rec_defs)} recurring templates")

        # Budgets — create for all periods (historical + current)
        print("\n7. Creating budgets...")
        budget_defs = [
            {"cat": "housing", "amount": "1400.00"},
            {"cat": "utilities", "amount": "250.00"},
            {"cat": "food_dining", "amount": "600.00"},
            {"cat": "transportation", "amount": "200.00"},
            {"cat": "health", "amount": "200.00"},
            {"cat": "lifestyle", "amount": "150.00"},
            {"cat": "personal_care", "amount": "100.00"},
        ]
        # Get all periods
        r = await c.get("/api/v1/settings/billing-periods", headers=headers)
        all_periods = r.json() if r.status_code == 200 else []
        for per in all_periods:
            ps = per["start_date"]
            for bd in budget_defs:
                if bd["cat"] in master_cats:
                    await c.post(f"/api/v1/budgets?period_start={ps}", headers=headers, json={
                        "category_id": master_cats[bd["cat"]], "amount": bd["amount"],
                    })
            print(f"   Budgets for period {ps}")

        print(f"\n=== Seed complete! ===")
        print(f"Login: {USER['username']} / {USER['password']}")


if __name__ == "__main__":
    asyncio.run(main())
