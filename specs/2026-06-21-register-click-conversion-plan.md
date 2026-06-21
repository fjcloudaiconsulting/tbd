# register_click Conversion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fire a GA4 `register_click` event when a visitor clicks any signup CTA on the apex marketing site, so Google Ads can import it as a conversion.

**Architecture:** One analytics helper (`trackRegisterClick`) calls the already-loaded `window.gtag` to emit a GA4 event; one shared client component (`SignupLink`) owns the click handler so the four server-rendered CTA sites don't need to become client components. The event uses GA4's `sendBeacon` transport, so firing on click survives the cross-domain navigation to the app host without delaying it. No CSP changes, no Google Ads tag, no Marketing-consent activation.

**Tech Stack:** Next.js 16 (App Router), React 19, TypeScript, Vitest + React Testing Library.

## Global Constraints

- Event name is exactly **`register_click`** — it is the contract the operator imports into Google Ads. Do not rename.
- Helper must **no-op** unless `isApexBuild` is true, `window` exists, and `window.gtag` is a function. Never throw.
- **Do not** delay or intercept navigation (no `event_callback`/`preventDefault`); rely on GA4 `sendBeacon`.
- **Do not** self-gate on consent — Consent Mode (already bootstrapped in `GoogleAnalytics.tsx`) handles redaction.
- No new external origins; no edits to `infra/terraform/apex/main.tf` CSP.
- Frontend verification gate (per repo): `npx tsc --noEmit` + `npx eslint . --quiet` + full `npx vitest run` must all pass. tsc/vitest green ≠ CI green; eslint is a separate gate.

---

## File Structure

- `frontend/lib/analytics.ts` (modify) — add `SignupCtaLocation` type + `trackRegisterClick()`. Owns the gtag call + guards.
- `frontend/components/landing/SignupLink.tsx` (create) — `"use client"` wrapper around `next/link` that fires the event on click. The single CTA primitive for signup buttons.
- `frontend/components/landing/{Hero,TopNav,SecondCta,VsPageLayout}.tsx` (modify) — swap the signup `<Link>` for `<SignupLink>`.
- `frontend/tests/lib/analytics.test.ts` (create) — unit tests for the helper.
- `frontend/tests/components/landing/signup-link.test.tsx` (create) — `SignupLink` renders an anchor + fires the helper on click.

---

### Task 1: `trackRegisterClick` helper

**Files:**
- Modify: `frontend/lib/analytics.ts` (append after line 16)
- Test: `frontend/tests/lib/analytics.test.ts` (create)

**Interfaces:**
- Produces: `export type SignupCtaLocation = "hero" | "topnav" | "second_cta" | "vs_page";` and `export function trackRegisterClick(location: SignupCtaLocation): void`.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/tests/lib/analytics.test.ts
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  delete (window as unknown as { gtag?: unknown }).gtag;
});

async function loadAnalytics(buildTarget?: string) {
  vi.resetModules();
  if (buildTarget) vi.stubEnv("NEXT_PUBLIC_BUILD_TARGET", buildTarget);
  return import("@/lib/analytics");
}

describe("trackRegisterClick", () => {
  it("fires a register_click GA4 event on the apex build when gtag exists", async () => {
    const gtag = vi.fn();
    (window as unknown as { gtag?: unknown }).gtag = gtag;
    const { trackRegisterClick } = await loadAnalytics("apex");
    trackRegisterClick("hero");
    expect(gtag).toHaveBeenCalledWith("event", "register_click", {
      cta_location: "hero",
    });
  });

  it("no-ops when not the apex build", async () => {
    const gtag = vi.fn();
    (window as unknown as { gtag?: unknown }).gtag = gtag;
    const { trackRegisterClick } = await loadAnalytics(); // unset target
    trackRegisterClick("hero");
    expect(gtag).not.toHaveBeenCalled();
  });

  it("does not throw when gtag is absent on the apex build", async () => {
    const { trackRegisterClick } = await loadAnalytics("apex");
    expect(() => trackRegisterClick("topnav")).not.toThrow();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec frontend npx vitest run tests/lib/analytics.test.ts`
Expected: FAIL — `trackRegisterClick` is not exported.

- [ ] **Step 3: Write minimal implementation**

Append to `frontend/lib/analytics.ts`:

```ts
export type SignupCtaLocation = "hero" | "topnav" | "second_cta" | "vs_page";

type GtagFn = (command: string, ...args: unknown[]) => void;

// Fire a GA4 event when a visitor clicks a signup CTA. The operator imports
// this event into Google Ads as the "register_click" conversion. GA4's
// sendBeacon transport lets the event survive the cross-domain navigation to
// the app host, so we never delay navigation. Consent Mode (bootstrapped in
// GoogleAnalytics.tsx) handles redaction — do not gate on consent here.
export function trackRegisterClick(location: SignupCtaLocation): void {
  if (!isApexBuild || typeof window === "undefined") return;
  const gtag = (window as unknown as { gtag?: GtagFn }).gtag;
  if (typeof gtag !== "function") return;
  gtag("event", "register_click", { cta_location: location });
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec frontend npx vitest run tests/lib/analytics.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/analytics.ts frontend/tests/lib/analytics.test.ts
git commit -m "feat(analytics): add trackRegisterClick GA4 event helper"
```

---

### Task 2: `SignupLink` client component

**Files:**
- Create: `frontend/components/landing/SignupLink.tsx`
- Test: `frontend/tests/components/landing/signup-link.test.tsx`

**Interfaces:**
- Consumes: `trackRegisterClick`, `SignupCtaLocation` (Task 1); `signupHref` (`@/lib/links`).
- Produces: `export default function SignupLink({ location, className, children }: { location: SignupCtaLocation; className?: string; children: ReactNode })`.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/tests/components/landing/signup-link.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SignupLink from "@/components/landing/SignupLink";
import { trackRegisterClick } from "@/lib/analytics";

vi.mock("@/lib/analytics", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/analytics")>()),
  trackRegisterClick: vi.fn(),
}));

afterEach(() => vi.clearAllMocks());

describe("SignupLink", () => {
  it("renders an anchor to the signup href", () => {
    render(<SignupLink location="hero" className="cta">Get started free</SignupLink>);
    const link = screen.getByRole("link", { name: "Get started free" });
    expect(link).toHaveAttribute("href", "/register"); // non-apex test build
    expect(link).toHaveClass("cta");
  });

  it("fires trackRegisterClick with its location on click", () => {
    render(<SignupLink location="topnav">Get started</SignupLink>);
    fireEvent.click(screen.getByRole("link", { name: "Get started" }));
    expect(trackRegisterClick).toHaveBeenCalledWith("topnav");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec frontend npx vitest run tests/components/landing/signup-link.test.tsx`
Expected: FAIL — module `@/components/landing/SignupLink` not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// frontend/components/landing/SignupLink.tsx
"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { signupHref } from "@/lib/links";
import { trackRegisterClick, type SignupCtaLocation } from "@/lib/analytics";

// Single signup-CTA primitive for the apex landing surface. Owns the
// register_click fire so the (server-rendered) call sites need not be client
// components. Navigation is the normal <Link> behaviour — the event rides
// GA4's sendBeacon and does not block it.
export default function SignupLink({
  location,
  className,
  children,
}: {
  location: SignupCtaLocation;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link
      href={signupHref()}
      className={className}
      onClick={() => trackRegisterClick(location)}
    >
      {children}
    </Link>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec frontend npx vitest run tests/components/landing/signup-link.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/components/landing/SignupLink.tsx frontend/tests/components/landing/signup-link.test.tsx
git commit -m "feat(landing): add SignupLink CTA primitive that fires register_click"
```

---

### Task 3: Wire the four signup CTA sites to `SignupLink`

**Files:**
- Modify: `frontend/components/landing/Hero.tsx`
- Modify: `frontend/components/landing/TopNav.tsx`
- Modify: `frontend/components/landing/SecondCta.tsx`
- Modify: `frontend/components/landing/VsPageLayout.tsx`

**Interfaces:**
- Consumes: `SignupLink` (Task 2).

- [ ] **Step 1: Hero.tsx** — replace the signup `<Link>` (lines ~30-35) and fix imports.

Change import line 3 from `import { signinHref, signupHref } from "@/lib/links";` to `import { signinHref } from "@/lib/links";`, and add `import SignupLink from "./SignupLink";`.

Replace:
```tsx
            <Link
              href={signupHref()}
              className={`${btnPrimary} px-6 py-3 text-base`}
            >
              Get started free
            </Link>
```
with:
```tsx
            <SignupLink
              location="hero"
              className={`${btnPrimary} px-6 py-3 text-base`}
            >
              Get started free
            </SignupLink>
```
(`Link` is still used for the Sign-in CTA — keep its import.)

- [ ] **Step 2: TopNav.tsx** — replace the signup `<Link>` (lines ~47-52) and fix imports.

Change import line 4 from `import { signinHref, signupHref } from "@/lib/links";` to `import { signinHref } from "@/lib/links";`, and add `import SignupLink from "./SignupLink";`.

Replace:
```tsx
        <Link
          href={signupHref()}
          className={`${btnPrimary} whitespace-nowrap`}
        >
          Get started
        </Link>
```
with:
```tsx
        <SignupLink location="topnav" className={`${btnPrimary} whitespace-nowrap`}>
          Get started
        </SignupLink>
```
(`Link` still used for Sign-in — keep its import.)

- [ ] **Step 3: SecondCta.tsx** — replace the only `<Link>` and drop now-unused imports.

Remove `import Link from "next/link";` and `import { signupHref } from "@/lib/links";`. Add `import SignupLink from "./SignupLink";`.

Replace:
```tsx
      <Link
        href={signupHref()}
        className={`${btnPrimary} mt-8 inline-block px-6 py-3 text-base`}
      >
        Get started free
      </Link>
```
with:
```tsx
      <SignupLink
        location="second_cta"
        className={`${btnPrimary} mt-8 inline-block px-6 py-3 text-base`}
      >
        Get started free
      </SignupLink>
```

- [ ] **Step 4: VsPageLayout.tsx** — replace the signup `<Link>` (line ~135) and fix imports.

Remove `import { signupHref } from "@/lib/links";` (line 14). Add `import SignupLink from "./SignupLink";`. (`Link` stays — it's still used for the `/compare` link.)

Replace:
```tsx
        <Link href={signupHref()} className={`${btnPrimary} mt-4 inline-flex items-center`}>
          Get started
        </Link>
```
with:
```tsx
        <SignupLink location="vs_page" className={`${btnPrimary} mt-4 inline-flex items-center`}>
          Get started
        </SignupLink>
```

- [ ] **Step 5: Type-check, lint, and run the FULL suite**

Run:
```bash
docker compose exec frontend npx tsc --noEmit
docker compose exec frontend npx eslint . --quiet
docker compose exec frontend npx vitest run
```
Expected: all green. The CTA swaps render the same anchors (href + text + classes unchanged), so existing Hero/TopNav/SecondCta/vs-page tests still pass. If a test asserted the element was a `next/link` internal, update it to assert the rendered anchor's `href`/text.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/landing/Hero.tsx frontend/components/landing/TopNav.tsx \
        frontend/components/landing/SecondCta.tsx frontend/components/landing/VsPageLayout.tsx
git commit -m "feat(landing): fire register_click from all signup CTAs"
```

---

## Self-Review

- **Spec coverage:** Engineering-design section of the spec (helper, shared component, sendBeacon, no-CSP, no-consent-gate, event shape `cta_location`, four call sites, testing) → Tasks 1–3. Campaign blueprint + runbook are operator console work, intentionally not in this code plan. ✔
- **Placeholder scan:** none. ✔
- **Type consistency:** `SignupCtaLocation` union (`hero|topnav|second_cta|vs_page`) defined in Task 1, consumed verbatim in Tasks 2–3; `trackRegisterClick(location)` signature consistent across helper, component, and tests; `register_click` event name + `cta_location` param consistent. ✔
```
