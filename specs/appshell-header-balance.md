---
name: AppShell Header Layout Balance
description: Rebalance AppShell header. Today's left-only cluster (trial banner + New Transaction CTA + docs + theme toggle) leaves the right side empty and looks lopsided. Use /impeccable critique when picking this up.
type: project
---
**Captured 2026-05-10.** Backlog item, sized S. Pure layout polish on AppShell header.

## Symptom

Today's AppShell header (post-#200) has all interactive elements clustered on the LEFT:
- Trial banner ("Trial ending today Upgrade")
- `+ New Transaction` brass CTA
- Docs help icon
- Theme toggle (sun/moon)

The RIGHT side is empty. The visual weight is lopsided. Users coming from other SaaS apps expect primary actions and secondary chrome to split between left (brand/nav) and right (user/account/actions).

Reference screenshot captured in operator's local notes (not in repo).

## What's blocking a quick fix

This isn't a "just move the button" decision. The header has competing tenants:
- The `+ New Transaction` CTA is the primary action (brass, frequent use, AppShell-mounted from #200).
- The trial banner is conditional and time-sensitive.
- The docs/help link is meta-navigation.
- The theme toggle is preference.
- A future feedback widget (per `project_inapp_feedback_widget.md`) will likely also live in this region.

Splitting these between left and right requires deciding the convention: what goes left, what goes right, what's the tab/keyboard order, what stays consistent across mobile/tablet/desktop, what becomes a dropdown menu.

## Recommended approach when picking up

Use `/impeccable critique` on the AppShell header layout. The impeccable plugin will:
- Run the cognitive load + Nielsen heuristics scoring.
- Surface the AppShell-specific anti-patterns (left-clustered actions, empty right side, ambiguous primary-action placement).
- Recommend a redesign that respects DESIGN.md's tokenized colors and PRODUCT.md's editorial-confident voice (one brass moment per screen).

Avoid invoking impeccable cold without context. The setup gates need PRODUCT.md + DESIGN.md context loaded; both are already in place (`load-context.mjs` runs).

## Likely answer (preview, do not commit)

Conventional split for a financial-planning product:
- **Left:** brand mark, primary nav (already there).
- **Right:** trial banner (when present), help icon, theme toggle, future feedback widget, future user menu / org switcher.
- **Primary CTA `+ New Transaction`:** could live either left-anchored (next to brand, "always-available add") or right-anchored (next to user actions, "this is your action"). The impeccable critique should pick.

Don't pre-commit to a layout — let the critique drive it.

## Acceptance criteria

- Run `/impeccable critique` on the AppShell header.
- Pick a layout that resolves the right-side emptiness.
- Apply changes via a focused PR. Single file (AppShell.tsx) ideally.
- Tests: existing AppShell tests still pass; a11y test confirms tab order is sensible.
- Mobile collapse behavior preserved (CTA still icon-only on narrow widths per #200's spec).
- Trial banner placement remains conditional (only shown during trial period).
- Brass focus rings preserved on all pressable elements (per DESIGN.md Pressable-Surfaces Rule).

## Why park

Visual polish, not a functional bug. Users can still access every action; they're just clustered. The impeccable critique should run when there's bandwidth to apply the recommendation properly, not as a rushed move.

## Cross-references

- PR #200 — AppShell CTA mounted with route-gated visibility
- `project_inapp_feedback_widget.md` — future feedback widget that will share this header region
- DESIGN.md — One Brass Rule, Tonal Depth Rule, Pressable-Surfaces Rule
- PRODUCT.md — editorial-confident voice; users open the app to make decisions
