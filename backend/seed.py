"""Seed script — populate the system with realistic mock data for testing.

Run with: docker compose exec backend python seed.py
Or via: ./pfv seed

Generates 3 past months + current month of data relative to today.
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


def billing_period_outcome(r: httpx.Response) -> str:
    """Interpret a ``POST /api/v1/settings/billing-period`` response.

    ``./pfv seed`` is documented as a repeatable dataset (CLAUDE.md,
    "Seeding"), and the period dates below are deterministic for a given
    ``today``, so a same-day re-run posts start dates that already exist.
    TBD-232 gave that endpoint a duplicate-start pre-flight that answers
    409 ``billing_period_exists``; treat that one status as "already
    seeded" and carry on, exactly like the login-instead-of-register and
    ``if r.status_code == 201`` account guards earlier in this script.

    TBD-239 added a second conflict code, ``billing_period_overlap``, and it
    has to be absorbed too. The dates below are anchored to ``today``, so a
    re-run on a LATER day shifts the whole window: the periods seeded
    yesterday are still there, and the ones being posted now land across
    them rather than exactly on them. That is a cross-day re-run answering
    409 ``billing_period_overlap``, and raising on it would abort ``./pfv
    seed`` at step 5, before recurring, budgets, forecast plans and reports.

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
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        print("=== PFV2 Seed Script ===\n")

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
        acct_defs = [
            {"name": "ING Checking", "type": "checking", "balance": "5000.00", "currency": "EUR"},
            {"name": "ING Savings", "type": "savings", "balance": "12000.00", "currency": "EUR"},
            {"name": "Amex Platinum", "type": "credit_card", "balance": "0.00", "currency": "EUR", "close_day": 15},
            {"name": "Revolut", "type": "checking", "balance": "800.00", "currency": "EUR"},
            {"name": "Degiro", "type": "investment", "balance": "25000.00", "currency": "EUR"},
        ]
        for ad in acct_defs:
            r = await c.post("/api/v1/accounts", headers=headers, json={
                "name": ad["name"], "account_type_id": account_types[ad["type"]],
                "balance": ad["balance"], "currency": ad["currency"], "close_day": ad.get("close_day"),
            })
            if r.status_code == 201:
                accounts[ad["name"]] = r.json()["id"]
                print(f"   {ad['name']} ({ad['balance']} {ad['currency']})")

        if "ING Checking" in accounts:
            await c.put(f"/api/v1/accounts/{accounts['ING Checking']}", headers=headers, json={"is_default": True})

        # Categories
        r = await c.get("/api/v1/categories", headers=headers)
        cats = {cat["slug"]: cat["id"] for cat in r.json() if cat["slug"]}
        master_cats = {cat["slug"]: cat["id"] for cat in r.json() if cat["parent_id"] is None and cat["slug"]}
        print(f"\n3. {len(cats)} categories loaded")

        # --- Transactions: 3 past months + current month ---
        print("\n4. Creating transactions...")
        today = date.today()
        tx_count = 0

        monthly_fixed = [
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

        variable = [
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

        # Generate for 3 past months + current month (4 total)
        for offset in range(3, -1, -1):
            m_start = today.replace(day=1) - relativedelta(months=offset)
            m_end = m_start + relativedelta(months=1) - timedelta(days=1)
            is_current = offset == 0

            # Salary (23rd-25th, only if date has passed)
            salary_day = random.choice([23, 24, 25])
            sal_date = m_start.replace(day=min(salary_day, 28))
            if sal_date <= today and "paycheck" in cats:
                await c.post("/api/v1/transactions", headers=headers, json={
                    "account_id": accounts["ING Checking"], "category_id": cats["paycheck"],
                    "description": "W&B Monthly Salary", "amount": "6500.00",
                    "type": "income", "status": "settled", "date": sal_date.isoformat(),
                })
                tx_count += 1

            # Side income (occasional)
            if random.random() > 0.5 and "side_hustles" in cats:
                si_date = m_start.replace(day=random.randint(10, 20))
                if si_date <= today:
                    await c.post("/api/v1/transactions", headers=headers, json={
                        "account_id": accounts["Revolut"], "category_id": cats["side_hustles"],
                        "description": "Freelance consulting", "amount": str(random.randint(200, 800)),
                        "type": "income", "status": "settled", "date": si_date.isoformat(),
                    })
                    tx_count += 1

            # Fixed expenses
            for exp in monthly_fixed:
                tx_date = m_start.replace(day=min(exp["day"], 28))
                if tx_date <= today and exp["cat"] in cats:
                    await c.post("/api/v1/transactions", headers=headers, json={
                        "account_id": accounts[exp["acct"]], "category_id": cats[exp["cat"]],
                        "description": exp["desc"], "amount": exp["amount"],
                        "type": "expense", "status": "settled", "date": tx_date.isoformat(),
                    })
                    tx_count += 1

            # Variable expenses (10-18 per month, spread across the month)
            num_var = random.randint(10, 18)
            for _ in range(num_var):
                exp = random.choice(variable)
                day = random.randint(1, min(today.day if is_current else 28, 28))
                tx_date = m_start.replace(day=day)
                if tx_date <= today and exp["cat"] in cats:
                    amount = round(random.uniform(exp["min"], exp["max"]), 2)
                    # Current month credit card = pending
                    status = "pending" if exp["acct"] == "Amex Platinum" and is_current else "settled"
                    await c.post("/api/v1/transactions", headers=headers, json={
                        "account_id": accounts[exp["acct"]], "category_id": cats[exp["cat"]],
                        "description": exp["desc"], "amount": str(amount),
                        "type": "expense", "status": status, "date": tx_date.isoformat(),
                    })
                    tx_count += 1

            # Monthly savings transfer (26th)
            xfer_date = m_start.replace(day=26)
            if xfer_date <= today and "general_savings" in cats:
                await c.post("/api/v1/transactions/transfer", headers=headers, json={
                    "from_account_id": accounts["ING Checking"],
                    "to_account_id": accounts["ING Savings"],
                    "category_id": cats["general_savings"],
                    "description": "Monthly savings", "amount": "500.00",
                    "status": "settled", "date": xfer_date.isoformat(),
                })
                tx_count += 2

            # Investment contribution (15th, bi-monthly)
            if offset % 2 == 0 and "investments" in cats:
                inv_date = m_start.replace(day=15)
                if inv_date <= today:
                    await c.post("/api/v1/transactions/transfer", headers=headers, json={
                        "from_account_id": accounts["ING Checking"],
                        "to_account_id": accounts["Degiro"],
                        "category_id": cats["investments"],
                        "description": "ETF investment", "amount": "300.00",
                        "status": "settled", "date": inv_date.isoformat(),
                    })
                    tx_count += 2

        print(f"   Created {tx_count} transactions")

        # Create explicit billing periods with varying salary days
        print("\n5. Creating billing periods...")
        period_defs = [
            # (start, end) — salary on 25th, 23rd, 24th
            (today.replace(day=1) - relativedelta(months=3) + timedelta(days=24),
             today.replace(day=1) - relativedelta(months=2) + timedelta(days=21)),  # 25th → 22nd next
            (today.replace(day=1) - relativedelta(months=2) + timedelta(days=22),
             today.replace(day=1) - relativedelta(months=1) + timedelta(days=22)),  # 23rd → 23rd next
            (today.replace(day=1) - relativedelta(months=1) + timedelta(days=23),
             today.replace(day=1) + timedelta(days=23)),  # 24th → 24th next (closed)
        ]
        for start, end in period_defs:
            if end < today:
                r = await c.post("/api/v1/settings/billing-period", headers=headers,
                                 json={"start_date": start.isoformat(), "end_date": end.isoformat()})
                outcome = billing_period_outcome(r)
                print(f"   Period: {start} — {end} ({outcome})")

        # Current open period (starts day after last closed)
        last_end = period_defs[-1][1] if period_defs[-1][1] < today else period_defs[-2][1]
        current_start = last_end + timedelta(days=1)
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
        next_month = today.replace(day=1) + relativedelta(months=1)
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
