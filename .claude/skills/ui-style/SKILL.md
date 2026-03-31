---
name: ui-style-enforcer
description: >
  Apply a consistent, professional, beautiful, and user-friendly UI style
  across frontend components in a personal finance system built with Next.js and
  TypeScript, using the FJ Consulting brand palette and visual identity. Use
  when designing, refactoring, reviewing, or generating UI, layouts, pages,
  dashboards, forms, tables, or reusable components.
---

# UI Style Enforcer

## Purpose

Ensure all frontend UI follows a consistent, modern, professional, and highly user-friendly design system.

This skill should be applied whenever:

- Creating new UI components
- Refactoring existing UI
- Reviewing frontend code
- Designing layouts or pages
- Building dashboards, tables, or financial workflows

The UI should prioritize:

- financial clarity
- app usability
- clean visual hierarchy
- consistent FJ Consulting branding

---

## Brand Foundation

Use the **FJ Consulting** visual identity as the base style system.

### Official Brand Palette

#### Primary Dark Navy
- Hex: `#0B1F3A`

Use for:
- main backgrounds
- app shell
- navigation
- headers
- premium dark sections

#### Primary Gold
- Hex: `#D4A64A`

Use for:
- primary CTA emphasis
- highlighted labels
- premium accents
- active states where appropriate

#### Off-White
- Hex: `#E6EAF0`

Use for:
- primary text on dark backgrounds
- panel contrast on dark surfaces
- subtle light UI elements

#### Secondary Blue
- Hex: `#5FA8D3`

Use for:
- links
- subtle informational accents
- charts or secondary UI emphasis

#### Dark Gold
- Hex: `#B88A2E`

Use for:
- hover states
- pressed states
- borders or deeper gold accents

---

## Core Design Principles

### 1. Financial Clarity First

- Data must be easy to scan quickly
- Numbers must stand out clearly
- Use strong alignment, especially right-aligned numeric values
- Important balances, totals, and trends must be visually obvious
- Users should understand their financial state at a glance

### 2. Professional and Clean

- Use a minimalist design language
- Avoid clutter
- Use whitespace generously
- Prioritize readability over decoration
- Prefer quiet confidence over flashy styling

### 3. Usability Over Branding

- The FJ brand palette should guide the interface
- Financial workflows must remain easy to use
- Never sacrifice readability, contrast, or navigation clarity for aesthetics
- Brand consistency should support the product, not dominate it

### 4. Consistency

- Reuse components whenever possible
- Use consistent spacing, typography, borders, radii, and visual hierarchy
- Follow a unified design system throughout the app

### 5. Accessibility

- Maintain strong contrast ratios
- Use clear labels and visible states
- Support keyboard navigation
- Avoid relying only on color to communicate meaning
- Pair color with labels, icons, or contextual text

---

## Visual Style Guidelines

### Color Usage Rules

#### Base Ratio
Prefer this overall visual balance:

- 70–80% navy / neutral surfaces
- 15–20% off-white / text / soft contrast areas
- 5–10% gold accent

Secondary blue should be used sparingly.

#### Semantic Colors
For financial meaning, semantic colors are allowed in addition to the core brand palette.

Recommended semantic accents:

- Success / income: green
- Danger / expenses: red
- Warning: amber
- Info: secondary blue

These colors should be used functionally, not decoratively.

#### Accent Discipline
- Gold should feel premium and deliberate
- Do not overuse gold in large blocks
- Use gold to draw attention to actions, totals, status, or key labels
- Avoid turning the app into a gold-heavy interface

#### Avoid
- loud gradients
- neon colors
- excessive color variety
- random brand drift
- overuse of saturated accents

---

## Typography

- Use modern sans-serif fonts
- Typography must feel professional, calm, and highly legible
- Favor strong hierarchy over decorative styling

### Hierarchy
- Page title: large, bold
- Section headers: medium, semibold
- Card titles: medium
- Body text: normal
- Labels: smaller, muted
- Numeric summaries: larger and visually emphasized

### Numeric Presentation
- Right-align values in tables
- Use tabular figures when available
- Make totals and balances visually distinct
- Use consistent currency formatting everywhere

---

## Spacing and Rhythm

- Use a consistent spacing scale such as 4px / 8px
- Avoid cramped layouts
- Add padding inside cards and panels
- Group related data tightly, but keep sections visually breathable
- Use spacing to separate meaning, not just elements

---

## Layout Patterns

### Dashboard

- Use cards to group information
- Put the most important financial information at the top
- Surface summary metrics first:
  - balance
  - income
  - expenses
  - savings or forecast
- Avoid excessive scrolling when possible
- Use clear sectioning and strong visual hierarchy

### Tables / Operations Lists

- Show a manageable number of items by default
- Prefer 10 items by default unless context requires otherwise
- Include:
  - pagination
  - filters
  - search
  - sorting when useful
- Right-align numeric values
- Highlight:
  - income in green
  - expenses in red
- Keep row actions visible and understandable
- Avoid dense, spreadsheet-like clutter unless explicitly requested

### Forms

- Keep forms simple and short
- Group related fields clearly
- Use inline validation where possible
- Use clear CTAs
- Provide helpful defaults when appropriate
- Reduce friction for common financial actions

### Navigation

- Keep navigation predictable
- Primary sections should be easy to discover
- Important user actions should not be buried
- Use active states clearly
- Prefer shallow navigation over deep nested flows

---

## Surface and Component Styling

### Cards
Use cards for grouped information.

Cards should:
- have clean padding
- avoid excessive shadows
- maintain clear hierarchy
- support quick scanning
- feel structured, not decorative

### Buttons

#### Primary Button
- background: `#D4A64A`
- text: `#0B1F3A`

#### Secondary Button
- darker neutral or transparent
- text: `#E6EAF0` on dark backgrounds
- optional border using `#D4A64A` or muted navy variants

Buttons should:
- have clear hover and active states
- be visually distinct
- not rely only on color to indicate priority

### Inputs
- clear labels
- visible focus states
- sufficient padding
- easy scanning in forms
- avoid overly stylized borders or animations

### Modals
- use only when needed
- keep them concise
- make close and confirm actions obvious
- avoid placing complex workflows inside modals unless necessary

### Charts
- keep charts simple and readable
- prefer clarity over novelty
- use brand colors and semantic colors intentionally
- avoid chartjunk, excessive labels, and unnecessary 3D or gradient effects

---

## Reusable Components

Always prefer reusable patterns and shared components where possible.

Core reusable components should include:

- Card
- Table
- Input field
- Select dropdown
- Modal
- Summary widget
- KPI tile
- Chart wrapper
- Empty state
- Loading state
- Error state
- Filter bar
- Pagination controls

When generating components:
- keep props clear
- avoid overengineering
- optimize for reuse and readability

---

## UX Rules

- Reduce friction
- Minimize clicks for frequent actions
- Always provide feedback:
  - success
  - error
  - loading
  - empty states
- Prioritize responsiveness and speed
- Keep interactions predictable
- Make critical financial actions feel safe and explicit

### UX Priorities
1. Understandable data
2. Fast task completion
3. Clear action paths
4. Reliable feedback
5. Visual consistency

---

## Dark and Light Behavior

The default visual identity should lean toward a **premium dark theme** anchored in navy.

If lighter screens are needed:
- use white or soft off-white backgrounds sparingly
- keep typography in navy or dark neutral tones
- preserve gold as a restrained accent
- maintain the same spacing, clarity, and financial readability rules

The UI should never become overly bright or generic.

---

## Enforcement Behavior

When this skill is active:

If generating UI:
- apply the FJ Consulting palette
- prioritize financial clarity and usability
- keep the design modern, minimal, and premium

If reviewing UI:
- identify readability problems
- identify spacing inconsistency
- identify weak hierarchy
- identify off-brand color usage
- suggest concrete fixes

If refactoring:
- improve UX without breaking familiarity
- preserve user mental models where possible
- strengthen consistency and visual clarity

If choosing between aesthetics and usability:
- always prioritize usability

---

## Output Expectations

When generating UI, always aim to provide:

- component structure
- layout rationale
- reusable component suggestions
- clear naming
- explanation of UX improvements
- alignment with the brand palette

When reviewing or refactoring UI:
- explain what should change
- explain why it should change
- preserve working patterns where sensible
- improve readability, hierarchy, and consistency

---

## Anti-Patterns (Avoid)

- overly complex UI
- cluttered dashboards
- too many colors
- inconsistent spacing
- hidden actions
- confusing navigation
- weak contrast
- gold overuse
- decorative visual noise
- charts that are hard to read
- financial data that is difficult to scan
- styling that looks generic, flashy, or trend-driven
