---
name: Accounts Page Side-by-Side Layout (follow-up to PR #199)
description: User suggestion 2026-05-10 to put Account Types and Accounts cards side-by-side again, but with reduced "Account" column widths so both cards fit cleanly without the cramping that made PR #199 necessary.
type: project
---
**Captured 2026-05-10.** PR #199 stacked the two cards vertically because the side-by-side layout cramped the Accounts list at lg+ viewports. User confirmed the stack works ("it's fixed"), but suggested an alternative shape:

> "We could definitely put the cards side by side using the same format you have now, but just reducing the 'Account' column width for both cards."

## Concept

Today's stacked layout (post-#199) uses each card's full content width. The Accounts list shows: Account name + type + close-day · Balance · Edit · Adjust balance · Set default · Deactivate · Delete. That's a lot of horizontal real estate per row.

The friend's suggestion: put Account Types and Accounts back side-by-side at lg+, but tighten the "Account" column on both so the action cluster fits.

## Open design questions

1. **What's the minimum Account column width** that doesn't truncate "ING Mastercard" + "Credit Card" + "closes day 5" on one line? Probably ~22ch with truncation.
2. **Action cluster width** — 5 actions at ~80px each is ~400px. Can compress to icon-only buttons with tooltips? Or hide-on-hover overflow-menu pattern?
3. **Account Types card sizing** — small (5 system rows in fresh installs). Side-by-side at 1:1 wastes left half. Maybe asymmetric grid: `grid-cols-[20rem_1fr]` so Types is ~320px and Accounts gets the remaining width.
4. **Tablet (768-1023)** — current PR #199 keeps it stacked. The side-by-side variant might still want to stack at md and only split at lg+ given remaining width.

## Why park it

PR #199 solved the immediate visual regression. The side-by-side variant is polish, not a bug fix. Sized S-M depending on whether action cluster collapse is needed. Not a launch blocker.

## Cross-references

- PR #199 (current stacked layout)
- PR #188 (Accounts list headers + fixed action column — still in effect post-#199)
- R1 responsive audit findings doc (mobile/tablet/desktop parity is the broader workstream)
