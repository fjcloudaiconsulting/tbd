---
name: finance-domain-architect
description: Enforce domain-driven design and correct financial modeling for a personal finance system. Use when designing features, schemas, APIs, or refactoring financial logic.
---

# Finance Domain Architect

## Purpose

Ensure the system follows a clean, scalable, and correct financial domain model.

---

## When to Use

- Designing new features
- Changing database schema
- Adding financial logic
- Refactoring backend services
- Reviewing architecture

---

## Core Entities (Required)

### Organization

- Multi-tenant boundary
- Owns all data

### User

- Belongs to organization
- Has roles and permissions

### Account

- Types: checking, savings, credit card, investment
- Balance must be derived (never manually set)

### Transaction

- Core entity
- Represents income, expense, or transfer
- Must include: amount, date, category, type, account
- Immutable (except controlled updates)

### Category

- Separate for income and expense
- Can be hierarchical

### Budget

- Planned financial allocation
- Period-based (monthly)

### Forecast

- Prediction layer (not real data)
- Based on recurring items + manual inputs
- Editable and savable

### Recurring Items

- Salary, rent, subscriptions
- Frequency-based

### Planned Expenses

- Future decisions
- Used for impact analysis
- Do NOT affect real balances

---

## Core Principles

### Separation of Concerns

Never mix:

- Transactions (actual)
- Budget (planned)
- Forecast (predicted)

---

### Derived Balances

- Always computed from transactions

---

### Time Awareness

- Transactions → timestamp
- Budget → period
- Forecast → future timeline

---

### Auditability

- Track changes
- Especially for AI actions

---

## Architecture Guidelines

### Backend Structure

/domains
/accounts
/transactions
/budgets
/forecasts
/categories
/ai

Each must contain:

- models
- schemas
- services
- repositories
- routes

---

## API Rules

- RESTful endpoints
- Clear separation:
  - /transactions
  - /budgets
  - /forecasts

---

## AI Rules

- AI must NOT mutate financial data directly
- Always create:
  - drafts
  - suggestions
- Require user approval

---

## Enforcement Behavior

- Reject designs that break financial consistency
- Refactor mixed concerns
- Normalize inconsistent schemas

---

## Anti-Patterns (Strictly Avoid)

- Storing balance directly
- Mixing forecast with real data
- Editing transactions freely
- Hardcoding logic in UI
- AI making silent changes

---

## Output Expectations

Always provide:

- Domain impact
- Data model changes
- API changes
- Business rules
- Consistency validation
