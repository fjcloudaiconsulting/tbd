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

BASE = "http://localhost:8000"

USER = {
    "username": os.getenv("SEED_USERNAME", "demo"),
    "email": os.getenv("SEED_EMAIL", "demo@example.com"),
    "password": os.getenv("SEED_PASSWORD", "demo1234"),
    "org_name": os.getenv("SEED_ORG", "Demo Household"),
}


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        print("=== PFV2 Seed Script ===\n")

        # Auth
        print("1. Authenticating...")
        r = await c.post("/api/v1/auth/login", json={"username": USER["username"], "password": USER["password"]})
        if r.status_code != 200:
            print("   User not found, registering...")
            r = await c.post("/api/v1/auth/register", json=USER)
            if r.status_code != 201:
                print(f"   Registration failed: {r.text}")
                return
            r = await c.post("/api/v1/auth/login", json={"username": USER["username"], "password": USER["password"]})

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

        # Historical billing periods with varying salary days
        print("\n5. Creating billing periods...")
        salary_days = [25, 23, 24]  # varying per month
        for i, offset in enumerate(range(3, 0, -1)):
            m = today.replace(day=1) - relativedelta(months=offset)
            sal_day = salary_days[i % len(salary_days)]
            period_start = date(m.year, m.month, sal_day)
            # Close the day before next salary
            next_m = m + relativedelta(months=1)
            next_sal_day = salary_days[(i + 1) % len(salary_days)]
            period_end = date(next_m.year, next_m.month, next_sal_day) - timedelta(days=1)
            if period_end <= today:
                await c.post("/api/v1/settings/billing-period/close", headers=headers,
                             params={"close_date": period_end.isoformat()})
                print(f"   Period: {period_start} — {period_end} (salary day {sal_day})")

        # Set default billing cycle day to 25
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

        # Budgets
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
        for bd in budget_defs:
            if bd["cat"] in master_cats:
                r = await c.post("/api/v1/budgets", headers=headers, json={
                    "category_id": master_cats[bd["cat"]], "amount": bd["amount"],
                })
                if r.status_code == 201:
                    print(f"   {bd['cat']} = {bd['amount']}")

        print(f"\n=== Seed complete! ===")
        print(f"Login: {USER['username']} / {USER['password']}")


if __name__ == "__main__":
    asyncio.run(main())
