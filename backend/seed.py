"""Seed script — populate the system with realistic mock data for testing.

Run with: docker compose exec backend python seed.py
Or via: ./pfv seed
"""

import asyncio
import random
from datetime import date, timedelta
from decimal import Decimal

import httpx

BASE = "http://localhost:8000"

# Test user
USER = {"username": "demo", "email": "demo@example.com", "password": "demo1234", "org_name": "Demo Household"}


async def main():
    async with httpx.AsyncClient(base_url=BASE) as c:
        print("=== PFV2 Seed Script ===\n")

        # Register
        print("1. Registering user...")
        r = await c.post("/api/v1/auth/register", json=USER)
        if r.status_code == 409:
            print("   User already exists, logging in...")
        elif r.status_code != 201:
            print(f"   Registration failed: {r.text}")
            return

        # Login
        r = await c.post("/api/v1/auth/login", json={"username": USER["username"], "password": USER["password"]})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"   Logged in as {USER['username']}")

        # Get account types
        r = await c.get("/api/v1/account-types", headers=headers)
        account_types = {at["slug"]: at["id"] for at in r.json() if at["slug"]}
        print(f"   Account types: {list(account_types.keys())}")

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
                "name": ad["name"],
                "account_type_id": account_types[ad["type"]],
                "balance": ad["balance"],
                "currency": ad["currency"],
                "close_day": ad.get("close_day"),
            })
            if r.status_code == 201:
                acct = r.json()
                accounts[ad["name"]] = acct["id"]
                print(f"   Created: {ad['name']} ({ad['balance']} {ad['currency']})")

        # Set default account
        if "ING Checking" in accounts:
            await c.put(f"/api/v1/accounts/{accounts['ING Checking']}", headers=headers,
                        json={"is_default": True})
            print("   Set ING Checking as default")

        # Get categories (subcategories for transactions)
        r = await c.get("/api/v1/categories", headers=headers)
        cats = {cat["slug"]: cat["id"] for cat in r.json() if cat["slug"]}
        master_cats = {cat["slug"]: cat["id"] for cat in r.json() if cat["parent_id"] is None and cat["slug"]}
        print(f"\n3. Categories loaded: {len(cats)} subcategories")

        # Create transactions — 3 months of data
        print("\n4. Creating transactions...")
        today = date.today()
        tx_count = 0

        # Monthly recurring patterns
        monthly_expenses = [
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

        # Variable expenses (credit card and checking)
        variable_expenses = [
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

        for month_offset in range(3, 0, -1):
            month_start = date(today.year, today.month - month_offset, 1) if today.month > month_offset else date(today.year - 1, 12 - (month_offset - today.month), 1)

            # Salary
            salary_day = random.choice([23, 24, 25])
            tx_date = date(month_start.year, month_start.month, salary_day)
            if tx_date <= today:
                await c.post("/api/v1/transactions", headers=headers, json={
                    "account_id": accounts["ING Checking"], "category_id": cats["paycheck"],
                    "description": "W&B Monthly Salary", "amount": "6500.00",
                    "type": "income", "status": "settled", "date": tx_date.isoformat(),
                })
                tx_count += 1

            # Monthly fixed expenses
            for exp in monthly_expenses:
                tx_date = date(month_start.year, month_start.month, min(exp["day"], 28))
                if tx_date <= today and exp["cat"] in cats:
                    await c.post("/api/v1/transactions", headers=headers, json={
                        "account_id": accounts[exp["acct"]], "category_id": cats[exp["cat"]],
                        "description": exp["desc"], "amount": exp["amount"],
                        "type": "expense", "status": "settled", "date": tx_date.isoformat(),
                    })
                    tx_count += 1

            # Variable expenses (8-15 per month)
            for _ in range(random.randint(8, 15)):
                exp = random.choice(variable_expenses)
                day = random.randint(1, 28)
                tx_date = date(month_start.year, month_start.month, day)
                if tx_date <= today and exp["cat"] in cats:
                    amount = round(random.uniform(exp["min"], exp["max"]), 2)
                    status = "pending" if exp["acct"] == "Amex Platinum" and month_offset == 1 else "settled"
                    await c.post("/api/v1/transactions", headers=headers, json={
                        "account_id": accounts[exp["acct"]], "category_id": cats[exp["cat"]],
                        "description": exp["desc"], "amount": str(amount),
                        "type": "expense", "status": status, "date": tx_date.isoformat(),
                    })
                    tx_count += 1

        # Monthly savings transfer
        for month_offset in range(3, 0, -1):
            month_start = date(today.year, today.month - month_offset, 1) if today.month > month_offset else date(today.year - 1, 12 - (month_offset - today.month), 1)
            tx_date = date(month_start.year, month_start.month, 26)
            if tx_date <= today:
                await c.post("/api/v1/transactions/transfer", headers=headers, json={
                    "from_account_id": accounts["ING Checking"],
                    "to_account_id": accounts["ING Savings"],
                    "category_id": cats.get("general_savings", list(cats.values())[0]),
                    "description": "Monthly savings", "amount": "500.00",
                    "status": "settled", "date": tx_date.isoformat(),
                })
                tx_count += 2  # two sides

        print(f"   Created {tx_count} transactions")

        # Create recurring transactions
        print("\n5. Creating recurring transactions...")
        recurring_defs = [
            {"acct": "ING Checking", "cat": "rent_mortgage", "desc": "Rent - Apartment", "amount": "1200.00", "freq": "monthly", "day": 1},
            {"acct": "ING Checking", "cat": "gym", "desc": "BasicFit Membership", "amount": "29.99", "freq": "monthly", "day": 1},
            {"acct": "ING Checking", "cat": "streaming", "desc": "Netflix", "amount": "17.99", "freq": "monthly", "day": 10},
            {"acct": "ING Checking", "cat": "streaming", "desc": "Spotify Family", "amount": "16.99", "freq": "monthly", "day": 10},
        ]
        for rd in recurring_defs:
            next_month = date(today.year, today.month + 1, 1) if today.month < 12 else date(today.year + 1, 1, 1)
            next_due = date(next_month.year, next_month.month, min(rd["day"], 28))
            if rd["cat"] in cats:
                await c.post("/api/v1/recurring", headers=headers, json={
                    "account_id": accounts[rd["acct"]], "category_id": cats[rd["cat"]],
                    "description": rd["desc"], "amount": rd["amount"],
                    "type": "expense", "frequency": rd["freq"],
                    "next_due_date": next_due.isoformat(), "auto_settle": True,
                })
        print(f"   Created {len(recurring_defs)} recurring templates")

        # Create budgets
        print("\n6. Creating budgets...")
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
                    print(f"   Budget: {bd['cat']} = {bd['amount']}")

        print("\n=== Seed complete! ===")
        print(f"Login: {USER['username']} / {USER['password']}")


if __name__ == "__main__":
    asyncio.run(main())
