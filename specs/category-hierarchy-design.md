---
name: Category Hierarchy Design
description: Hierarchical categories — master categories for budgets, subcategories as transaction tags
type: project
---

## Structure
- **Master categories** = top-level (no parent_id) — used for budgeting and report grouping
- **Subcategories** = child categories (parent_id set) — used as tags on transactions
- Transactions reference subcategories; master category derived via parent relationship

## Schema additions to categories table
- `parent_id` — nullable, self-referencing FK
- `description` — hint text to help users understand the category
- `is_system` + `slug` — seeded on registration, protected from deletion

## System-seeded categories (12 master + ~50 subcategories)

| Master | Subcategories |
|--------|--------------|
| Income | Paycheck/Salary, Side Hustles, Bonuses, Interest/Dividends, Tax Refunds |
| Housing | Rent/Mortgage, Property Taxes, Home Insurance, HOA Fees, Repairs/Maintenance |
| Utilities | Electricity, Water, Gas, Internet, Phone, Trash/Recycling |
| Food & Dining | Groceries, Restaurants, Coffee Shops, Fast Food/Takeout |
| Transportation | Fuel/Gas, Car Payments, Auto Insurance, Public Transit, Maintenance/Repairs, Parking/Tolls |
| Health & Wellness | Health Insurance, Doctor Visits/Copays, Pharmacy/Meds, Gym Membership, Dental/Vision |
| Personal Care | Haircuts, Toiletries, Clothing, Shoes, Laundry/Dry Cleaning |
| Lifestyle & Fun | Streaming Services, Movies/Concerts, Hobbies, Travel/Vacation, Books/Media |
| Financial Goals | Emergency Fund, Retirement (401k/IRA), General Savings, Brokerage Investments |
| Debt Repayment | Credit Cards, Student Loans, Personal Loans |
| Giving & Gifts | Charitable Donations, Birthday/Holiday Gifts |
| Miscellaneous | Bank Fees, Taxes (Non-Property), Uncategorized/Unexpected |

## Type mapping
- Income master → type=income, all its subcategories → type=income
- All other masters → type=expense, subcategories → type=expense
- Custom categories can be type=both

## Future use
- Budgets allocate at master category level
- Reports can drill down from master → subcategory
- Charts group by master, filter by subcategory

**Why:** Rich categorization enables meaningful budgets, reports, and charts. Master/sub split gives flexibility without forcing users into too-granular or too-broad categories.
