---
name: Audit Unused Preloaded CSS Warning (Next.js Production)
description: P3 backlog. Chrome reports `_next/static/chunks/...css` was preloaded but not used shortly after load on /dashboard. Most likely Next.js-generated CSS chunking, not first-party. Investigate before changing config.
type: project
originSessionId: 0bf77f16-13ef-4926-8a64-7a5ddd96efc6
---
**Captured 2026-05-10.** P3 backlog item. Raise to P2 only if many repeated CSS preload warnings appear OR measurable dashboard performance impact (LCP, FOUC, layout shift, extra transfer size).

## Background — what's already correct

Repo audit confirmed: app only imports `frontend/app/layout.tsx:5` for global CSS. No manual `<link rel="preload">` injection in first-party code. Next.js production builds automatically split CSS into route chunks. The warning is therefore most likely Next-generated, not a bug in our code.

## What NOT to do

**Do not disable CSS chunking or change Next config just to silence the warning.** That trades a console warning for a real performance regression (one big CSS file instead of route-split chunks). Any config change requires evidence of measurable impact.

## Investigation steps

1. **Reproduce in production** after hard refresh (Cmd+Shift+R) in Incognito with extensions disabled. Local dev's HMR-injected CSS won't reproduce this.
2. **Capture the exact `<link rel="preload">` tag** from page source / DevTools Elements. Confirm it carries `as="style"` (the warning is specific to that combination).
3. **Identify the owning route/component** via DevTools Network → Initiator OR by inspecting `.next/build-manifest.json` / `.next/static/chunks/` mappings.
4. **Verify metric impact:**
   - LCP delta with/without the preloaded chunk
   - FOUC visible during route transitions
   - Cumulative Layout Shift (CLS)
   - Network transfer size (the wasted bytes if the chunk is preloaded but never applied)
5. **Decide based on findings:**
   - If the CSS chunk comes from an accidental route-level / global CSS import that's reaching pages that don't need it: reduce import scope (move the CSS to the route that actually uses it).
   - If the warning is purely Next-generated and no metric or visual issue exists: document as benign and close. Console warnings without functional impact aren't bugs.

## Acceptance criteria

- Reproduced (or not) in production with extensions disabled.
- DevTools capture: link tag, route, owning chunk, metric deltas.
- If first-party scope can be reduced: PR opened with the import-scope fix.
- If benign: closed with a one-paragraph rationale documented in this file.

## Priority

- **P3 by default** — pure console noise without proof of real-world impact.
- **P2** if multiple repeated CSS preload warnings stack up OR a measurable dashboard performance impact lands in DevTools / synthetic monitoring.

## Cross-references

- `frontend/app/layout.tsx` — the single global CSS import path
- Next.js build manifests under `frontend/.next/` — for owning-route attribution
- DESIGN.md — page CSS scope rules (cards / forms / etc. live in tokenized utility classes, not per-route stylesheets)

## Why it lives in backlog rather than active queue

Console warnings are not bugs. The product's seed user opens the app to make decisions, not to inspect console output (per PRODUCT.md). Real-user functional impact has to lead any config change to Next.js's default CSS chunking behavior.
