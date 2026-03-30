---
name: ui-style-enforcer
description: >
  Apply a consistent, professional, beautiful, and user-friendly UI style
  across all frontend components. Use when designing, refactoring, or reviewing
  UI in a personal finance system built with Next.js and TypeScript.
---

# UI Style Enforcer

## Purpose

Ensure all frontend UI follows a consistent, modern, professional, and highly user-friendly design system.

This skill should be applied whenever:

- Creating new UI components
- Refactoring existing UI
- Reviewing frontend code
- Designing layouts or pages

---

## Core Design Principles

### 1. Professional & Clean

- Minimalist design
- Avoid clutter
- Use whitespace generously
- Prioritize readability over decoration

### 2. Financial Clarity

- Data must be easy to scan quickly
- Numbers should stand out clearly
- Use alignment (especially right-aligned numbers)

### 3. Consistency

- Reuse components whenever possible
- Use consistent spacing, typography, and colors
- Follow a unified design system

### 4. Accessibility

- High contrast ratios
- Clear labels and form inputs
- Keyboard navigation support
- Avoid relying only on color for meaning

---

## Visual Style Guidelines

### Colors

- Primary: neutral + calm (blue/indigo tones)
- Success (income): green
- Danger (expenses): red
- Warning: amber
- Background: light gray / white
- Text: dark gray (not pure black)

### Typography

- Use modern sans-serif fonts
- Clear hierarchy:
  - Title: large, bold
  - Section headers: medium, semi-bold
  - Body: normal
  - Labels: smaller, muted

### Spacing

- Use consistent spacing scale (e.g., 4px/8px grid)
- Avoid tight layouts
- Use padding inside cards

---

## Layout Patterns

### Dashboard

- Use cards for grouping information
- Keep most important info at the top
- Avoid long vertical scrolling when possible

### Tables (Operations List)yes
Yes
- Show max 10 items by default
- Include:
  - Pagination
  - Filters
  - Search
- Highlight:
  - Income in green
  - Expenses in red

### Forms

- Keep forms simple and short
- Use inline validation
- Group related fields
- Use clear CTAs (e.g., “Add Expense”)

---

## Components

Always prefer reusable components:

- Card
- Table
- Input field
- Select dropdown
- Modal
- Summary widget (income/expense/balance)
- Charts (simple, readable)

---

## UX Rules

- Reduce friction (few clicks to complete actions)
- Always provide feedback (success, error, loading)
- Avoid blocking the user unnecessarily
- Prioritize speed and responsiveness

---

## Anti-Patterns (Avoid)

- Overly complex UI
- Too many colors
- Inconsistent spacing
- Hidden actions
- Confusing navigation
- Financial data that is hard to read

---

## Output Expectations

When generating UI:

- Provide component structure
- Suggest layout improvements
- Use clear naming
- Prefer reusable patterns
- Follow the style rules above strictly

If refactoring:

- Explain what is improved and why
- Maintain original layout where possible
- Improve UX without breaking familiarity
