---
name: CSP Console Warning Audit
description: Investigate browser CSP warning (script-src blocked eval/string evaluation). DO NOT add 'unsafe-eval' to production CSP. Investigate first; the warning is most likely dev-only or third-party.
type: project
originSessionId: 0bf77f16-13ef-4926-8a64-7a5ddd96efc6
---
**Captured 2026-05-10.** Backlog item, P2 priority. Raise to P1 only if reproduced in deployed production AND breaks functionality.

## Background — what's already correct

`frontend/next.config.ts:20` already does the right broad thing:
- **Dev:** allows `'unsafe-eval'` (Next.js / React debugging needs this).
- **Production:** does NOT allow `'unsafe-eval'`.

Repo scan found NO first-party usage of `eval`, `new Function`, string-form `setTimeout`, or string-form `setInterval`. Next.js docs explicitly note `unsafe-eval` may be needed in development for React debugging but should not be in production by default.

## What NOT to do

**Do not add `'unsafe-eval'` to production CSP.** That's the wrong fix. Loosening the policy because of a console warning, without first identifying the source, weakens defense-in-depth. Any decision to relax production CSP requires explicit security review that accepts the specific risk.

## Investigation steps (the right fix)

1. **Reproduce against deployed production.** `https://app.thebetterdecision.com`, not local dev. Local dev has `'unsafe-eval'` so the warning won't fire there.
2. **Capture full DevTools details:**
   - Blocked URI
   - Source file, line/column
   - Route where it fires
   - Browser + version
   - Does anything actually BREAK, or is it pure console noise?
3. **Confirm production CSP still omits `'unsafe-eval'`.** Re-read `frontend/next.config.ts:20` and the deployed response headers (`curl -I https://app.thebetterdecision.com` and check `Content-Security-Policy`).
4. **Confirm first-party code has no offenders:**
   - `git grep -nE "\\beval\\s*\\(|new\\s+Function\\s*\\(" frontend/`
   - `git grep -nE "setTimeout\\s*\\(\\s*['\"]|setInterval\\s*\\(\\s*['\"]" frontend/`
5. **Identify the source bucket:**
   - **Browser extension?** Test in Incognito with extensions disabled. If the warning vanishes, it's a user-environment issue, not a product bug. Close as not-a-bug.
   - **Third-party script (analytics, embeds, GSI button)?** Identify the script source and decide: remove, replace with a CSP-clean alternative, or isolate via iframe / nonce-allowed inline.
   - **First-party bundle issue?** Webpack/Next.js sometimes injects `Function()` for code-splitting helpers; validate via the source file name in the warning.
6. **Optional hardening (independent of root-cause fix):** add CSP `report-uri` / `report-to` in report-only mode so violations are collected centrally instead of manually discovered. This is a small, separate PR worth shipping regardless.

## Acceptance criteria

- Warning reproduced (or not) against deployed production, not local dev.
- DevTools capture filed with all five fields above.
- Production CSP confirmed to still omit `'unsafe-eval'`.
- First-party code grep confirmed clean.
- If third-party: removed, replaced, or isolated. NOT papered over with CSP relaxation.
- Closing the item documents which path (browser extension / third-party / first-party / Next.js internal) was the cause.

## Priority

- P2 by default.
- P1 if reproduced in deployed production AND a real functional break is observed (not just a console warning).
- If the warning is dev-only or extension-only: close as not-a-bug.

## Short brief for the dev who picks this up

Investigate and document the CSP violation. Do not add `'unsafe-eval'` to production. The current config (`unsafe-eval` allowed in dev only) is correct; if the warning fires in prod, find the source and fix it there, not in the CSP.

## Cross-references

- `frontend/next.config.ts` — the CSP construction (line 20 area for the dev/prod split)
- Pentest history (per `code_sweep_*.md` memories) — CSP hardening was an explicit goal
- DESIGN.md — no specific CSP rule, but the broader "production stays strict" posture is consistent with the project's locked rules
