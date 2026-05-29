# Hide Payments + Baseline SEO + Ollama LAN Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three independent PRs that (A) unblock LAN/loopback IPs for Ollama credentials, (B) hide all remaining payment/billing/upgrade surfaces, and (C) ship baseline SEO so the marketing site is indexable and the rest of the app is not.

**Architecture:** Three phases, one PR per phase, no inter-PR dependency — implementer can ship in any order. PR A includes the design spec commit. PR B extends the existing `billing_ui_enabled` flag (shipped 2026-05-21) to its remaining gaps without introducing new infrastructure. PR C flips the default `robots` directive in the root layout from `index` to `noindex` and opts in the 7 indexable public routes — fewer file touches and a safer default than opt-out-per-page.

**Tech Stack:** FastAPI / SQLAlchemy 2.0 / Pydantic v2 (backend), Next.js 15 App Router / React 19 / Vitest (frontend), Docker Compose (test runner per CLAUDE.md).

**Spec:** `specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md` — read it first; this plan implements that spec exactly.

---

## File Structure (across all three PRs)

### PR A — Ollama validator
- **Modify** `backend/app/schemas/org_ai_credential.py:29-130` — split IP guard into two helpers, move provider-conditional check into model validator.
- **Modify** `backend/tests/schemas/test_org_ai_credential_ssrf.py` — extend with the 9-case test matrix.
- **Commit** `specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md` (the design spec, hitching a ride on the smallest PR).
- **Commit** `specs/2026-05-29-hide-payments-seo-baseline-ollama-lan-plan.md` (this plan).

### PR B — Hide remaining payment surfaces
- **Delete** `frontend/components/landing/PricingPreview.tsx`
- **Modify** `frontend/app/page.tsx` — remove `PricingPreview` import + render.
- **Modify** `frontend/components/landing/Faq.tsx` — delete 2 payment entries, strip "(Pro tier)".
- **Modify** `frontend/components/landing/Hero.tsx` — add "Free while in beta" line near CTA.
- **Modify** `frontend/components/AppShell.tsx` — gate Subscriptions + Plan Catalog admin nav entries on `billingUiEnabled`.
- **Modify** `backend/app/services/subscription_service.py:86` — gate `_send_trial_email_safe` call on `app_settings.billing_ui_enabled`.
- **Modify** `backend/tests/services/test_email_templates.py` (or new file) — add gating tests.
- **Create** `frontend/tests/landing-payments-hidden.test.tsx` — render assertions for the deleted surfaces.
- **Create** `frontend/tests/appshell-admin-nav-billing-gate.test.tsx` — admin nav assertions for both flag states.

### PR C — Baseline SEO
- **Modify** `frontend/app/layout.tsx:28-31` — flip default `robots` to `{ index: false, follow: false }`.
- **Modify** the 7 indexable pages (`/`, `/login`, `/register`, `/privacy`, `/terms`, `/docs`, `/docs/plans`) — add `robots: { index: true, follow: true }` to each page's existing `metadata` export.
- **Modify** `frontend/app/sitemap.ts` — append `/docs` and `/docs/plans`.
- **Modify** `frontend/components/landing/Hero.tsx` — `<h1>` audit + tune.
- **Modify** `frontend/app/page.tsx` — extend JSON-LD with `author` / `publisher` and add a sibling `FAQPage` block.
- **Verify** per-page metadata distinctness on the 7 indexable routes (small fixes if any inherit the root template).
- **Create** `specs/seo-admin-config-backlog.md` — backlog doc for the future per-route SEO admin UI.
- **Create** `frontend/tests/root-layout-robots-default.test.tsx` — root layout default is noindex.
- **Create** `frontend/tests/seo-public-routes-indexable.test.tsx` — opt-in pages override to index.
- **Create** `frontend/tests/seo-flow-routes-noindex.test.tsx` — inheritance check on a flow-only route.
- **Create** `frontend/tests/landing-jsonld-faqpage.test.tsx` — FAQPage block + author/publisher presence.
- **Create** `frontend/tests/sitemap-includes-docs.test.ts` — sitemap.ts contains /docs.

---

## Conventions for all phases

**Test runner (per CLAUDE.md):**
- Backend tests: `docker compose exec backend pytest <path>`
- Frontend tests: `docker compose exec frontend npm test -- <path>`
- TypeScript check: `docker compose exec frontend npx tsc --noEmit`

**This is the user's local stack** (not a parallel agent session), so plain `docker compose exec` without `-p team-<name>` is correct. If executing as a subagent dispatched in parallel, the implementer MUST follow `~/.claude/projects/-Users-flamarion-src-tbd/memory/reference_shared_mysql_volume_trap.md` and isolate the compose project with `-p team-<name>`.

**Branch naming:** `fix/ollama-lan-ip-allow`, `feat/hide-remaining-payment-surfaces`, `feat/baseline-seo-noindex-default`.

**PR style (per [[feedback_pr_format]] + [[feedback_no_ai_attribution]]):** Concise description, no test-plan section, no Co-Authored-By.

**Commit style:** Frequent, small, with `feat`/`fix`/`test`/`chore` prefix. Never push to main. Always PR.

---

# PHASE A — PR A: Ollama validator allows LAN/loopback IPs

**Goal:** Backend Pydantic schema accepts `http://192.168.1.163:11434/` and `http://127.0.0.1:11434/` (and IPv6 loopback `[::1]`) when `provider=ollama`, while still rejecting cloud metadata IPs and link-local. Other providers retain the strict block.

**Branch:** `fix/ollama-lan-ip-allow`

---

### Task A.1: Create branch off main

**Files:** none yet.

- [ ] **Step 1: Confirm main is clean and current**

```bash
git fetch origin
git status
```
Expected: clean working tree, on any branch.

- [ ] **Step 2: Branch off origin/main**

```bash
git checkout -b fix/ollama-lan-ip-allow origin/main
```
Expected: `Switched to a new branch 'fix/ollama-lan-ip-allow'`.

---

### Task A.2: Commit the design spec + plan (PR A carries these)

**Files:**
- Add: `specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md` (already on disk, untracked)
- Add: `specs/2026-05-29-hide-payments-seo-baseline-ollama-lan-plan.md` (this plan, already on disk, untracked)

- [ ] **Step 1: Verify both files are present**

```bash
ls specs/2026-05-29-hide-payments-seo-baseline-ollama-lan*.md
```
Expected: both files listed.

- [ ] **Step 2: Stage and commit**

```bash
git add specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md specs/2026-05-29-hide-payments-seo-baseline-ollama-lan-plan.md
git commit -m "spec: hide payments + baseline SEO + Ollama LAN allow"
```

---

### Task A.3: Write failing test for Ollama LAN IP acceptance

**Files:**
- Test: `backend/tests/schemas/test_org_ai_credential_ssrf.py`

- [ ] **Step 1: Inspect existing test file structure**

```bash
docker compose exec backend cat backend/tests/schemas/test_org_ai_credential_ssrf.py | head -60
```
Expected output: shows the existing import block, fixtures, and test naming convention. Use the same style and helpers below.

- [ ] **Step 2: Append the new test cases at the end of the file**

```python
# --- 2026-05-29: Ollama LAN/loopback policy ---

import pytest
from pydantic import ValidationError
from app.schemas.org_ai_credential import OrgAICredentialCreate
from app.models.org_ai_credential import AiProvider


@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.168.1.163:11434/",   # RFC1918 192.168/16
        "http://10.0.0.5:11434/",        # RFC1918 10/8
        "http://172.16.5.5:11434/",      # RFC1918 172.16/12
        "http://127.0.0.1:11434/",       # loopback IPv4
        "http://[::1]:11434/",           # loopback IPv6
        "http://[::ffff:192.168.1.1]/",  # IPv4-mapped IPv6 LAN
    ],
)
def test_ollama_accepts_lan_and_loopback_ip(base_url):
    cred = OrgAICredentialCreate(
        provider=AiProvider.OLLAMA,
        base_url=base_url,
    )
    assert cred.base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254/",                   # AWS/GCP/DO IMDS
        "http://[fd00:ec2::254]/",                   # AWS IPv6 IMDS
        "http://[::ffff:169.254.169.254]/",          # mapped IPv6 metadata
        "http://169.254.1.1/",                       # link-local non-metadata
        "http://224.0.0.1/",                         # multicast
    ],
)
def test_ollama_still_rejects_metadata_and_unsafe(base_url):
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OLLAMA,
            base_url=base_url,
        )


def test_openai_compatible_still_rejects_lan_ip():
    """Strict SSRF block unchanged for non-Ollama providers."""
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-test-key-1234",
            base_url="http://192.168.1.163/",
        )


def test_public_ip_still_accepted_for_any_provider():
    cred_ollama = OrgAICredentialCreate(
        provider=AiProvider.OLLAMA,
        base_url="https://ollama.example.com/",
    )
    cred_openai = OrgAICredentialCreate(
        provider=AiProvider.OPENAI_COMPATIBLE,
        api_key="sk-test-key-1234",
        base_url="https://api.example.com/",
    )
    assert cred_ollama.base_url.endswith(".example.com/")
    assert cred_openai.base_url.endswith(".example.com/")
```

- [ ] **Step 3: Run new tests to confirm they fail (current schema rejects LAN for Ollama)**

```bash
docker compose exec backend pytest backend/tests/schemas/test_org_ai_credential_ssrf.py -v -k "lan_and_loopback or still_rejects_metadata or openai_compatible_still or public_ip"
```
Expected: `test_ollama_accepts_lan_and_loopback_ip[…]` FAIL with ValidationError; the "rejects" tests likely PASS already (current strict block); `test_openai_compatible_still_rejects_lan_ip` PASS; `test_public_ip_still_accepted_for_any_provider` PASS.

---

### Task A.4: Refactor `_reject_private_ip_literal` into two helpers

**Files:**
- Modify: `backend/app/schemas/org_ai_credential.py:29-87`

- [ ] **Step 1: Replace the single helper with two split helpers**

Replace lines 29-69 (`_reject_private_ip_literal`) with:

```python
def _ip_or_none(host: str) -> "ipaddress._BaseAddress | None":
    """Return the parsed IP if ``host`` is a literal address (with IPv4-mapped
    IPv6 unwrapped to its IPv4 form), or None if it's a DNS name."""
    if not host:
        return None
    candidate = host.strip("[]")
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def _reject_metadata_or_unsafe(host: str) -> None:
    """Always-blocked classes (safe for all providers including Ollama):
    cloud-metadata IPs, link-local (covers the rest of 169.254/16 beyond
    the metadata constant), multicast, unspecified, and reserved.

    DNS names pass through (see module docstring for the DNS rebinding
    note)."""
    ip = _ip_or_none(host)
    if ip is None:
        return
    if str(ip) in _METADATA_IPS:
        raise ValueError("base_url cannot point at a cloud metadata endpoint")
    if (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        raise ValueError(
            "base_url cannot point at a link-local, multicast, "
            "unspecified, or reserved IP"
        )


def _reject_private_or_loopback(host: str) -> None:
    """RFC1918 (10/8, 172.16/12, 192.168/16) and loopback (127.0.0.0/8, ::1).
    Blocked for hosted providers; allowed for Ollama (operator's own LAN /
    homelab — see spec 2026-05-29 section 3)."""
    ip = _ip_or_none(host)
    if ip is None:
        return
    if ip.is_private or ip.is_loopback:
        raise ValueError(
            "base_url cannot point at a private (RFC1918) or loopback IP"
        )
```

- [ ] **Step 2: Update `_validate_base_url` to only call the always-blocked helper**

Replace lines 72-87 with:

```python
def _validate_base_url(value: str) -> str:
    """Reject base_url values that open an SSRF surface, regardless of
    provider. Provider-conditional checks (RFC1918 / loopback) run in
    the model validator where ``provider`` is known.

    Allowed: http/https scheme + public hostname/IP. Private DNS names
    (``ollama.internal``, ``my-llm.local``) ARE allowed — operators
    fronting Ollama in their VPC need them. DNS rebinding remains a
    residual v1 risk; a future iteration can add a custom httpx
    transport that re-checks the resolved address before connect.
    """
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url must use http or https scheme")
    if not parsed.hostname:
        raise ValueError("base_url must include a hostname")
    _reject_metadata_or_unsafe(parsed.hostname)
    return value
```

- [ ] **Step 3: Add the provider-conditional block to `_check_provider_requirements`**

Inside the model validator at line ~110, immediately after the existing `base_url is required for ollama and openai_compatible providers` check, add:

```python
        # Provider-conditional SSRF policy:
        # - Ollama: operator's own LAN/homelab, allow RFC1918 + loopback.
        # - All other providers: strict block per the v1 SSRF guard.
        if self.base_url and self.provider != AiProvider.OLLAMA:
            parsed = urlparse(self.base_url)
            if parsed.hostname:
                _reject_private_or_loopback(parsed.hostname)
```

This belongs **after** the `base_url is required` check (so the missing-base_url ValueError fires first) and **before** the bearer_token / api_key checks.

- [ ] **Step 4: Run the new tests to confirm they pass**

```bash
docker compose exec backend pytest backend/tests/schemas/test_org_ai_credential_ssrf.py -v
```
Expected: all tests in the file PASS, including the new ones from Task A.3.

- [ ] **Step 5: Run the full schemas test suite to confirm no regressions**

```bash
docker compose exec backend pytest backend/tests/schemas/ -v
```
Expected: every test PASSES.

- [ ] **Step 6: Run the AI credential service + crypto tests too (touch-test)**

```bash
docker compose exec backend pytest backend/tests/services/test_ai_credential_service.py backend/tests/services/test_ai_credential_crypto.py -v
```
Expected: all PASS.

---

### Task A.5: Commit and push the branch

- [ ] **Step 1: Verify diff**

```bash
git diff --stat
```
Expected: 2 files changed — `backend/app/schemas/org_ai_credential.py` and `backend/tests/schemas/test_org_ai_credential_ssrf.py`.

- [ ] **Step 2: Commit**

```bash
git add backend/app/schemas/org_ai_credential.py backend/tests/schemas/test_org_ai_credential_ssrf.py
git commit -m "$(cat <<'EOF'
fix(ai): allow RFC1918 + loopback for Ollama base_url

Spec: specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md §3

The 2026-05-22 BYO credentials SSRF guard rejects all literal private
IPs unconditionally. PR #375 shipped LAN-only Ollama (no api_key) but
left the IP literal guard intact, so the most natural LAN URL
(http://192.168.1.163:11434/) hits a Pydantic ValidationError.

Split _reject_private_ip_literal into two helpers:
- _reject_metadata_or_unsafe — always blocked (metadata, link-local,
  multicast, unspecified, reserved). Runs in the field validator.
- _reject_private_or_loopback — RFC1918 + loopback. Runs in the
  model validator only for non-Ollama providers.

Behavior change is one-directional: previously rejected Ollama LAN
URLs now accept. Previously accepted URLs continue to accept. Strict
SSRF block for openai_compatible / anthropic / openai unchanged.
EOF
)"
```

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin fix/ollama-lan-ip-allow
gh pr create --title "fix(ai): allow RFC1918 + loopback for Ollama base_url" --body "$(cat <<'EOF'
## Summary
- Splits the SSRF IP guard into always-blocked vs provider-conditional helpers.
- Ollama credentials now accept RFC1918 LAN + loopback (incl. `[::1]` and IPv4-mapped IPv6) — unblocks the LAN-only homelab mode shipped in PR #375.
- Cloud metadata IPs (`169.254.169.254`, `fd00:ec2::254`) and link-local remain hard-blocked for ALL providers, including Ollama.
- Strict block for `openai_compatible` / `anthropic` / `openai` is unchanged.

## Spec
specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md §3
EOF
)"
```
Expected: PR URL printed.

---

# PHASE B — PR B: Hide remaining payment surfaces

**Goal:** With `BILLING_UI_ENABLED=false` (prod default), zero user-visible payment references anywhere. Landing surfaces hardcode-deleted (apex is static); in-app + backend surfaces flag-gated.

**Branch:** `feat/hide-remaining-payment-surfaces`

---

### Task B.1: Create branch off main

- [ ] **Step 1:**

```bash
git fetch origin && git checkout -b feat/hide-remaining-payment-surfaces origin/main
```

---

### Task B.2: Write failing test for landing page payment-string absence

**Files:**
- Create: `frontend/tests/landing-payments-hidden.test.tsx`

- [ ] **Step 1: Identify the existing landing-page test pattern**

```bash
docker compose exec frontend ls tests/ | grep -i landing
```
Expected: lists any existing landing tests, e.g., `landing-hero.test.tsx`. Mimic the import + render style.

- [ ] **Step 2: Create the new test**

```typescript
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import LandingPage from "@/app/page";

describe("landing page after payment hide", () => {
  it("has zero references to pricing/plans/payment", async () => {
    const ui = await LandingPage();
    const { container } = render(ui as React.ReactElement);
    const text = container.textContent ?? "";

    const forbidden = [
      "Pro",
      "Team",
      "€9",
      "€19",
      "Coming soon",
      "Join the waitlist",
      "Pricing",
      "payment methods",
      "free plan",
    ];

    for (const needle of forbidden) {
      expect(text.toLowerCase()).not.toContain(needle.toLowerCase());
    }
  });
});
```

- [ ] **Step 3: Run to confirm it fails**

```bash
docker compose exec frontend npm test -- tests/landing-payments-hidden.test.tsx
```
Expected: FAIL — at least "Pro", "Team", "€9", "Coming soon", "Join the waitlist" still appear (from PricingPreview).

---

### Task B.3: Delete `PricingPreview.tsx` and remove the import

**Files:**
- Delete: `frontend/components/landing/PricingPreview.tsx`
- Modify: `frontend/app/page.tsx`

- [ ] **Step 1: Find the import + render site in page.tsx**

```bash
grep -n "PricingPreview" /Users/flamarion/src/tbd/frontend/app/page.tsx
```
Expected: two matches — one import line near the top, one `<PricingPreview />` tag in the JSX.

- [ ] **Step 2: Delete the file**

```bash
git rm frontend/components/landing/PricingPreview.tsx
```

- [ ] **Step 3: Remove import + render from `page.tsx`**

Use Edit to remove the import line and the `<PricingPreview />` JSX tag. The component sits between other landing sections — leave the surrounding sections untouched.

- [ ] **Step 4: Re-run the landing test**

```bash
docker compose exec frontend npm test -- tests/landing-payments-hidden.test.tsx
```
Expected: most forbidden strings are now gone; "free plan" + "payment methods" may still fail (from `Faq.tsx`).

---

### Task B.4: Delete payment FAQ entries and strip "(Pro tier)"

**Files:**
- Modify: `frontend/components/landing/Faq.tsx`

- [ ] **Step 1: Inspect the FAQ data structure**

```bash
sed -n '1,80p' /Users/flamarion/src/tbd/frontend/components/landing/Faq.tsx
```
Expected: shows the FAQ array. Locate:
- The entry with question containing "payment methods".
- The entry with question containing "free plan" (in the "Is there a free plan?" sense — NOT the free-tier reference inside the AI assistant line).
- The line referring to "AI assistant (Pro tier)".

- [ ] **Step 2: Delete the two entries from the array**

Use Edit on `Faq.tsx` to remove the two objects (`{ q: "What payment methods...", a: "..." }` and `{ q: "Is there a free plan?", a: "..." }`). Preserve the trailing comma discipline of the array (no dangling commas, no missing ones).

- [ ] **Step 3: Strip "(Pro tier)" from the AI-assistant line**

In the AI-assistant feature description, change `optional AI assistant (Pro tier)` → `optional AI assistant`. Keep the rest of the sentence intact.

- [ ] **Step 4: Re-run the landing test**

```bash
docker compose exec frontend npm test -- tests/landing-payments-hidden.test.tsx
```
Expected: PASS (all forbidden strings absent).

- [ ] **Step 5: Quick visual sanity (TypeScript + tsc)**

```bash
docker compose exec frontend npx tsc --noEmit
```
Expected: no type errors.

---

### Task B.5: Add "Free while in beta" line near the Hero CTA

**Files:**
- Modify: `frontend/components/landing/Hero.tsx`

- [ ] **Step 1: Find the primary CTA in the hero**

```bash
grep -n "Get started\|Create your\|signup\|sign up\|register\|Try" /Users/flamarion/src/tbd/frontend/components/landing/Hero.tsx
```
Expected: shows the CTA `<Link>` or `<a>` element and surrounding markup.

- [ ] **Step 2: Add the line immediately below the CTA**

The exact JSX depends on Hero's structure. Add a paragraph or span with these EXACT classes if Tailwind is used (match siblings) and this EXACT copy:

```tsx
<p className="mt-3 text-sm text-foreground/70">
  Free while in beta. No credit card required.
</p>
```

If a different classname pattern is already used (e.g., `text-muted-foreground`, `text-slate-500`), follow that pattern. Keep the copy verbatim.

- [ ] **Step 3: Render-check the landing**

```bash
docker compose exec frontend npm test -- tests/landing-payments-hidden.test.tsx
```
Expected: still PASS (the new line doesn't contain any forbidden strings).

---

### Task B.6: Commit the landing changes

- [ ] **Step 1:**

```bash
git add -u frontend/components/landing/PricingPreview.tsx frontend/app/page.tsx frontend/components/landing/Faq.tsx frontend/components/landing/Hero.tsx frontend/tests/landing-payments-hidden.test.tsx
git commit -m "feat(landing): hide pricing + payment FAQs, add beta notice"
```

---

### Task B.7: Write failing test for AppShell admin nav gating

**Files:**
- Create: `frontend/tests/appshell-admin-nav-billing-gate.test.tsx`

- [ ] **Step 1: Find how AppShell is tested today**

```bash
docker compose exec frontend ls tests/ | grep -i appshell
```
Expected: lists existing AppShell tests. Borrow the auth-context mock pattern.

- [ ] **Step 2: Create the test**

```typescript
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import AppShell from "@/components/AppShell";

const makeSuperadminAuth = (billingUiEnabled: boolean) => ({
  user: {
    id: 1,
    email: "a@b",
    is_superadmin: true,
    org_role: "owner",
  },
  billingUiEnabled,
  // … fill in other required AuthContext fields with sensible defaults
});

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

import { useAuth } from "@/components/auth/AuthProvider";

describe("AppShell admin nav billing gate", () => {
  it("hides Subscriptions and Plan Catalog when billingUiEnabled=false", () => {
    (useAuth as any).mockReturnValue(makeSuperadminAuth(false));
    render(<AppShell><div /></AppShell>);
    expect(screen.queryByText("Subscriptions")).toBeNull();
    expect(screen.queryByText("Plan Catalog")).toBeNull();
  });

  it("shows Subscriptions and Plan Catalog when billingUiEnabled=true", () => {
    (useAuth as any).mockReturnValue(makeSuperadminAuth(true));
    render(<AppShell><div /></AppShell>);
    expect(screen.getByText("Subscriptions")).toBeInTheDocument();
    expect(screen.getByText("Plan Catalog")).toBeInTheDocument();
  });
});
```

NOTE: the `makeSuperadminAuth` helper needs the full shape of `AuthContextValue`. Open `frontend/components/auth/AuthProvider.tsx`, copy the value type, and fill in defaults for any required field not shown above. Don't ship a `TODO: fill in fields` comment.

- [ ] **Step 3: Run to confirm failure**

```bash
docker compose exec frontend npm test -- tests/appshell-admin-nav-billing-gate.test.tsx
```
Expected: the first test FAILS (Subscriptions/Plan Catalog visible when flag is off); the second test PASSES.

---

### Task B.8: Gate the two admin nav entries in `AppShell.tsx`

**Files:**
- Modify: `frontend/components/AppShell.tsx:170-200` (the admin nav array)

- [ ] **Step 1: Locate the admin nav declaration**

```bash
sed -n '160,210p' /Users/flamarion/src/tbd/frontend/components/AppShell.tsx
```
Expected: shows the array literal containing Subscriptions and Plan Catalog entries (lines ~178 and ~184-188 per the inventory).

- [ ] **Step 2: Confirm `billingUiEnabled` is already destructured from `useAuth()`**

If not, add it. Pattern (mirrors how SettingsLayout does it):

```tsx
const { user, billingUiEnabled } = useAuth();
```

- [ ] **Step 3: Filter the two entries**

Two equivalent edits — pick whichever matches the local style:

**Option 1 (inline conditional spread):**

```tsx
const adminNav = [
  // … other admin entries unchanged
  ...(billingUiEnabled
    ? [
        { label: "Subscriptions", href: "/admin/subscriptions", icon: CreditCard },
        { label: "Plan Catalog", href: "/system/plans", icon: CreditCard },
      ]
    : []),
  // … other admin entries unchanged
];
```

**Option 2 (post-build filter):**

```tsx
const adminNav = [
  // … all entries including Subscriptions + Plan Catalog
].filter(item => {
  if (!billingUiEnabled && (item.href === "/admin/subscriptions" || item.href === "/system/plans")) {
    return false;
  }
  return true;
});
```

Match the codebase style — if `SettingsLayout` uses Option 2 (post-build filter), use Option 2 here too for consistency. Check first:

```bash
grep -A6 "billingUiEnabled" /Users/flamarion/src/tbd/frontend/components/SettingsLayout.tsx
```

- [ ] **Step 4: Re-run the test**

```bash
docker compose exec frontend npm test -- tests/appshell-admin-nav-billing-gate.test.tsx
```
Expected: both tests PASS.

- [ ] **Step 5: TS check**

```bash
docker compose exec frontend npx tsc --noEmit
```
Expected: clean.

---

### Task B.9: Commit the AppShell changes

```bash
git add frontend/components/AppShell.tsx frontend/tests/appshell-admin-nav-billing-gate.test.tsx
git commit -m "feat(appshell): gate Subscriptions + Plan Catalog admin nav on billingUiEnabled"
```

---

### Task B.10: Write failing test for trial-email gating

**Files:**
- Modify: `backend/tests/services/test_email_templates.py` (extend) OR create `backend/tests/services/test_subscription_service_trial_email_gate.py`.

Choose **extend** if `test_email_templates.py` already imports `app_settings`; otherwise create a new file to keep the email-template-formatting tests separate from gating logic tests. Per the inventory it's likely cleaner to create a new file.

- [ ] **Step 1: Create the new test file**

```python
"""Test that send_trial_expiring_email is gated by billing_ui_enabled."""
from unittest.mock import AsyncMock, patch

import pytest

from app.services import subscription_service


@pytest.mark.asyncio
async def test_send_trial_email_safe_no_op_when_flag_off(monkeypatch):
    """When BILLING_UI_ENABLED=false, the trial reminder email is not sent."""
    monkeypatch.setattr(
        "app.services.subscription_service.app_settings.billing_ui_enabled",
        False,
    )
    sent = AsyncMock()
    monkeypatch.setattr(
        "app.services.subscription_service.send_trial_expiring_email",
        sent,
    )
    await subscription_service._send_trial_email_safe(
        email="u@x.com",
        days_left=3,
        org_name="Doe Household",
    )
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_send_trial_email_safe_sends_when_flag_on(monkeypatch):
    """When BILLING_UI_ENABLED=true, the trial reminder email IS sent."""
    monkeypatch.setattr(
        "app.services.subscription_service.app_settings.billing_ui_enabled",
        True,
    )
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.subscription_service.send_trial_expiring_email",
        sent,
    )
    await subscription_service._send_trial_email_safe(
        email="u@x.com",
        days_left=3,
        org_name="Doe Household",
    )
    sent.assert_called_once_with("u@x.com", 3, "Doe Household")
```

- [ ] **Step 2: Confirm `app_settings` is imported in subscription_service.py**

```bash
grep -n "app_settings\|from app.config" /Users/flamarion/src/tbd/backend/app/services/subscription_service.py | head
```
If `app_settings` is not yet imported, the next step's edit will add the import.

- [ ] **Step 3: Run the new tests to confirm they fail**

```bash
docker compose exec backend pytest backend/tests/services/test_subscription_service_trial_email_gate.py -v
```
Expected: `test_send_trial_email_safe_no_op_when_flag_off` FAILS (send IS called); `test_send_trial_email_safe_sends_when_flag_on` PASSES.

---

### Task B.11: Gate `_send_trial_email_safe` on `app_settings.billing_ui_enabled`

**Files:**
- Modify: `backend/app/services/subscription_service.py:82-90`

- [ ] **Step 1: Add the import if absent**

If `app_settings` is not already imported at the top of the file, add:

```python
from app.config import settings as app_settings
```

(Use the same alias name `app_settings` so the test's monkeypatch path matches.)

- [ ] **Step 2: Add the gate inside `_send_trial_email_safe`**

Replace the existing function body with:

```python
async def _send_trial_email_safe(email: str, days_left: int, org_name: str) -> None:
    """Send trial reminder in background; swallow errors to avoid unhandled task exceptions.

    No-op when billing UI is disabled (the user never sees a way to act
    on the email; sending it would be confusing during the pre-payment
    beta window).
    """
    if not app_settings.billing_ui_enabled:
        return
    try:
        await send_trial_expiring_email(email, days_left, org_name)
    except Exception:
        await logger.awarning("trial_reminder_email_failed", email=email, days_left=days_left)
```

- [ ] **Step 3: Run the gating tests**

```bash
docker compose exec backend pytest backend/tests/services/test_subscription_service_trial_email_gate.py -v
```
Expected: both PASS.

- [ ] **Step 4: Run the existing email-template tests (touch-test for regressions)**

```bash
docker compose exec backend pytest backend/tests/services/test_email_templates.py -v
```
Expected: all PASS (template formatting is unaffected; we only gated the caller).

- [ ] **Step 5: Run the subscription_service test suite**

```bash
docker compose exec backend pytest backend/tests/services/ -v -k "subscription"
```
Expected: all PASS.

---

### Task B.12: Commit the backend changes

```bash
git add backend/app/services/subscription_service.py backend/tests/services/test_subscription_service_trial_email_gate.py
git commit -m "feat(billing): gate _send_trial_email_safe on billing_ui_enabled"
```

---

### Task B.13: Push and open PR

```bash
git push -u origin feat/hide-remaining-payment-surfaces
gh pr create --title "feat: hide remaining payment surfaces (landing + admin nav + trial email)" --body "$(cat <<'EOF'
## Summary
- Deletes the landing pricing section and the two payment-related FAQ entries; adds a "Free while in beta" notice to the hero.
- Gates the admin-nav "Subscriptions" and "Plan Catalog" entries on the existing `billingUiEnabled` flag (same pattern as `SettingsLayout`'s Billing tab).
- Gates `_send_trial_email_safe` on `app_settings.billing_ui_enabled` so we stop sending trial-expiring emails while the customer-facing billing surface is hidden.
- Landing-surface edits are hardcoded (apex landing is a static export and can't read the runtime flag) — revert via `git revert` when payment is wired.

## Spec
specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md §1
EOF
)"
```

---

# PHASE C — PR C: Baseline SEO

**Goal:** Marketing landing + 6 other public pages are indexable with distinct meta; everything else (auth-walled + flow-only public routes) is `noindex` by default. Sitemap covers public docs. Landing emits two JSON-LD blocks (`SoftwareApplication` + `FAQPage`).

**Branch:** `feat/baseline-seo-noindex-default`

**Strategy note:** No `(app)` route group exists; flipping the root layout to `noindex` default and opting-in 7 pages is fewer files (8) and a safer default than adding noindex to 25 individual routes.

---

### Task C.1: Create branch off main

```bash
git fetch origin && git checkout -b feat/baseline-seo-noindex-default origin/main
```

---

### Task C.2: Write failing test — root layout default is noindex

**Files:**
- Create: `frontend/tests/root-layout-robots-default.test.tsx`

- [ ] **Step 1: Create the test**

```typescript
import { describe, it, expect } from "vitest";
import { metadata } from "@/app/layout";

describe("root layout default robots", () => {
  it("defaults to noindex, nofollow (safer for an auth-walled app)", () => {
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });
});
```

- [ ] **Step 2: Run to confirm failure**

```bash
docker compose exec frontend npm test -- tests/root-layout-robots-default.test.tsx
```
Expected: FAIL — current root metadata.robots is `{ index: true, follow: true }`.

---

### Task C.3: Flip root layout default

**Files:**
- Modify: `frontend/app/layout.tsx:28-31`

- [ ] **Step 1: Replace the robots block**

Change

```typescript
  robots: {
    index: true,
    follow: true,
  },
```

to

```typescript
  // Safer default for an auth-walled SaaS: noindex by default.
  // The 7 indexable public pages (/, /login, /register, /privacy,
  // /terms, /docs, /docs/plans) opt back in via their own metadata.
  robots: {
    index: false,
    follow: false,
  },
```

- [ ] **Step 2: Re-run the test**

```bash
docker compose exec frontend npm test -- tests/root-layout-robots-default.test.tsx
```
Expected: PASS.

---

### Task C.4: Write failing test — indexable pages opt back in

**Files:**
- Create: `frontend/tests/seo-public-routes-indexable.test.tsx`

- [ ] **Step 1: Create the test**

```typescript
import { describe, it, expect } from "vitest";
import { metadata as rootMetadata } from "@/app/page";
import { metadata as loginMetadata } from "@/app/login/page";
import { metadata as registerMetadata } from "@/app/register/page";
import { metadata as privacyMetadata } from "@/app/privacy/page";
import { metadata as termsMetadata } from "@/app/terms/page";
import { metadata as docsMetadata } from "@/app/docs/page";
import { metadata as docsPlansMetadata } from "@/app/docs/plans/page";

const indexableMetadatas = [
  ["/", rootMetadata],
  ["/login", loginMetadata],
  ["/register", registerMetadata],
  ["/privacy", privacyMetadata],
  ["/terms", termsMetadata],
  ["/docs", docsMetadata],
  ["/docs/plans", docsPlansMetadata],
] as const;

describe("indexable public routes opt back into index", () => {
  it.each(indexableMetadatas)("%s sets robots index/follow true", (_route, meta) => {
    expect(meta.robots).toEqual({ index: true, follow: true });
  });
});
```

- [ ] **Step 2: Run to confirm failure**

```bash
docker compose exec frontend npm test -- tests/seo-public-routes-indexable.test.tsx
```
Expected: FAIL — none of the pages explicitly set `robots: { index: true }`.

---

### Task C.5: Add `robots: { index: true, follow: true }` to the 7 indexable pages

**Files:**
- Modify each of:
  - `frontend/app/page.tsx`
  - `frontend/app/login/page.tsx`
  - `frontend/app/register/page.tsx`
  - `frontend/app/privacy/page.tsx`
  - `frontend/app/terms/page.tsx`
  - `frontend/app/docs/page.tsx`
  - `frontend/app/docs/plans/page.tsx`

- [ ] **Step 1: For each page, locate the existing `export const metadata` block**

```bash
for f in frontend/app/page.tsx frontend/app/login/page.tsx frontend/app/register/page.tsx frontend/app/privacy/page.tsx frontend/app/terms/page.tsx frontend/app/docs/page.tsx frontend/app/docs/plans/page.tsx; do
  echo "=== $f ==="
  grep -n "export const metadata\|robots:" /Users/flamarion/src/tbd/$f
done
```
Expected: each page shows an `export const metadata = { ... }` line. None should currently have a `robots:` key.

- [ ] **Step 2: Add the robots key to each page's metadata object**

For each file, use Edit to add (preserving surrounding properties):

```typescript
  robots: { index: true, follow: true },
```

Place it adjacent to other top-level meta keys (e.g., after `title` / `description`). If the page uses a helper like `pageSocialMeta()` that spreads its return value, add the `robots` key OUTSIDE the spread (so it's not overridden):

```typescript
export const metadata: Metadata = {
  ...pageSocialMeta({ /* … */ }),
  robots: { index: true, follow: true },
};
```

- [ ] **Step 3: Re-run the test**

```bash
docker compose exec frontend npm test -- tests/seo-public-routes-indexable.test.tsx
```
Expected: all 7 cases PASS.

---

### Task C.6: Write failing test — flow-only routes inherit noindex

**Files:**
- Create: `frontend/tests/seo-flow-routes-noindex.test.tsx`

- [ ] **Step 1: Create the test**

```typescript
import { describe, it, expect } from "vitest";
import { metadata as forgotMetadata } from "@/app/forgot-password/page";
import { metadata as resetMetadata } from "@/app/reset-password/page";
import { metadata as verifyMetadata } from "@/app/verify-email/page";
import { metadata as mfaMetadata } from "@/app/mfa-verify/page";
import { metadata as onboardingMetadata } from "@/app/onboarding/page";
import { metadata as acceptInviteMetadata } from "@/app/accept-invite/page";

// These pages should NOT opt back into index — they inherit root's noindex.
describe("flow-only public routes are not indexed", () => {
  const flowMetadatas = [
    ["/forgot-password", forgotMetadata],
    ["/reset-password", resetMetadata],
    ["/verify-email", verifyMetadata],
    ["/mfa-verify", mfaMetadata],
    ["/onboarding", onboardingMetadata],
    ["/accept-invite", acceptInviteMetadata],
  ] as const;

  it.each(flowMetadatas)("%s does not opt back into index", (_route, meta) => {
    // Either no robots key (inherit root noindex) or explicit noindex.
    if (meta.robots !== undefined) {
      expect(meta.robots).toEqual({ index: false, follow: false });
    }
  });
});
```

- [ ] **Step 2: Run**

```bash
docker compose exec frontend npm test -- tests/seo-flow-routes-noindex.test.tsx
```
Expected: PASS (these pages don't currently set `robots`, so they inherit the now-noindex root). If any page does set `robots: { index: true }`, the test FAILS and you must remove or correct that override.

---

### Task C.7: Commit Tasks C.2–C.6

```bash
git add frontend/app/layout.tsx frontend/app/page.tsx frontend/app/login/page.tsx frontend/app/register/page.tsx frontend/app/privacy/page.tsx frontend/app/terms/page.tsx frontend/app/docs/page.tsx frontend/app/docs/plans/page.tsx frontend/tests/root-layout-robots-default.test.tsx frontend/tests/seo-public-routes-indexable.test.tsx frontend/tests/seo-flow-routes-noindex.test.tsx
git commit -m "feat(seo): default to noindex, opt 7 public routes back into index"
```

---

### Task C.8: Write failing test — sitemap includes `/docs` and `/docs/plans`

**Files:**
- Create: `frontend/tests/sitemap-includes-docs.test.ts`

- [ ] **Step 1: Create the test**

```typescript
import { describe, it, expect } from "vitest";
import sitemap from "@/app/sitemap";

describe("sitemap.ts", () => {
  it("includes /docs and /docs/plans", () => {
    const urls = sitemap().map(entry => new URL(entry.url).pathname);
    expect(urls).toContain("/docs");
    expect(urls).toContain("/docs/plans");
  });

  it("preserves the original 5 public URLs", () => {
    const urls = sitemap().map(entry => new URL(entry.url).pathname);
    expect(urls).toContain("/");
    expect(urls).toContain("/login");
    expect(urls).toContain("/register");
    expect(urls).toContain("/privacy");
    expect(urls).toContain("/terms");
  });
});
```

- [ ] **Step 2: Run**

```bash
docker compose exec frontend npm test -- tests/sitemap-includes-docs.test.ts
```
Expected: first test FAILS (sitemap currently has 5 URLs, none of them /docs); second PASSES.

---

### Task C.9: Append docs URLs to `sitemap.ts`

**Files:**
- Modify: `frontend/app/sitemap.ts`

- [ ] **Step 1: Inspect current sitemap**

```bash
cat /Users/flamarion/src/tbd/frontend/app/sitemap.ts
```

- [ ] **Step 2: Add the two new entries**

Append two entries with priority 0.4 (support content, not conversion):

```typescript
  {
    url: `${siteUrl}/docs`,
    lastModified: new Date(),
    changeFrequency: "monthly",
    priority: 0.4,
  },
  {
    url: `${siteUrl}/docs/plans`,
    lastModified: new Date(),
    changeFrequency: "monthly",
    priority: 0.4,
  },
```

Match the existing import + `siteUrl` helper used by the file. Field names (`changeFrequency` casing, etc.) must match Next.js's `MetadataRoute.Sitemap` type — copy from the existing entries above.

- [ ] **Step 3: Re-run**

```bash
docker compose exec frontend npm test -- tests/sitemap-includes-docs.test.ts
```
Expected: both PASS.

---

### Task C.10: Write failing test — Hero has exactly one `<h1>`

**Files:**
- Create: `frontend/tests/hero-single-h1.test.tsx`

- [ ] **Step 1: Create the test**

```typescript
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import Hero from "@/components/landing/Hero";

describe("Hero", () => {
  it("renders exactly one <h1>", () => {
    const { container } = render(<Hero />);
    expect(container.querySelectorAll("h1").length).toBe(1);
  });

  it("h1 contains keyword-friendly copy", () => {
    const { container } = render(<Hero />);
    const h1 = container.querySelector("h1");
    expect(h1?.textContent ?? "").toMatch(/finance|money|budget|plan/i);
  });
});
```

- [ ] **Step 2: Run**

```bash
docker compose exec frontend npm test -- tests/hero-single-h1.test.tsx
```
Expected: either PASS already (if Hero has one h1 with keyword content) or FAIL (audit-and-fix in next task).

---

### Task C.11: Hero `<h1>` audit + tune

**Files:**
- Modify: `frontend/components/landing/Hero.tsx` (only if needed)

- [ ] **Step 1: Open Hero.tsx and count `<h1>` elements**

```bash
grep -c "<h1" /Users/flamarion/src/tbd/frontend/components/landing/Hero.tsx
```

- [ ] **Step 2: Apply fixes based on Step 1**

- If count == 0: add an `<h1>` containing the starter copy `"Personal finance, planned not panicked"` (operator may finalize the exact phrase before merge).
- If count == 1 and the test from Task C.10 passes: no change.
- If count > 1: demote extras to `<h2>` while keeping their styling. Re-run the test.

- [ ] **Step 3: Re-run**

```bash
docker compose exec frontend npm test -- tests/hero-single-h1.test.tsx
```
Expected: both tests PASS.

---

### Task C.12: Verify per-page metadata distinctness (audit + small fixes)

**Files:**
- Read: each of the 7 indexable pages' `metadata` block.
- Modify: only those that just inherit the root template (no own `title` / `description`).

- [ ] **Step 1: Print each page's title + description**

```bash
for f in frontend/app/page.tsx frontend/app/login/page.tsx frontend/app/register/page.tsx frontend/app/privacy/page.tsx frontend/app/terms/page.tsx frontend/app/docs/page.tsx frontend/app/docs/plans/page.tsx; do
  echo "=== $f ==="
  sed -n '/export const metadata/,/^};/p' /Users/flamarion/src/tbd/$f
done
```

- [ ] **Step 2: Confirm each page has distinct, keyword-relevant `title` + `description`**

For each page, the title should be unique (not just the root template's default) and reflect the page's intent. The description should be 130-160 chars, mention the page's topic, and include search-relevant terms.

If a page is missing distinct metadata, add it. Example for `/login`:

```typescript
export const metadata: Metadata = {
  title: "Sign in",  // becomes "Sign in · The Better Decision" via root template
  description: "Sign in to The Better Decision to manage your accounts, budgets, and financial plans.",
  robots: { index: true, follow: true },
};
```

NOTE: do NOT replicate the root description on every page — that triggers duplicate-description warnings in Search Console.

- [ ] **Step 3: Commit Tasks C.8–C.12 (sitemap + Hero + metadata audit)**

```bash
git add frontend/app/sitemap.ts frontend/components/landing/Hero.tsx \
  frontend/app/page.tsx frontend/app/login/page.tsx frontend/app/register/page.tsx \
  frontend/app/privacy/page.tsx frontend/app/terms/page.tsx \
  frontend/app/docs/page.tsx frontend/app/docs/plans/page.tsx \
  frontend/tests/sitemap-includes-docs.test.ts \
  frontend/tests/hero-single-h1.test.tsx
git commit -m "feat(seo): expand sitemap with docs, audit Hero h1, distinct per-page meta"
```

---

### Task C.13: Write failing test — landing JSON-LD has SoftwareApplication + FAQPage

**Files:**
- Create: `frontend/tests/landing-jsonld-faqpage.test.tsx`

- [ ] **Step 1: Create the test**

```typescript
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import LandingPage from "@/app/page";

describe("landing JSON-LD", () => {
  it("renders SoftwareApplication and FAQPage blocks", async () => {
    const ui = await LandingPage();
    const { container } = render(ui as React.ReactElement);
    const scripts = Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    );
    expect(scripts.length).toBeGreaterThanOrEqual(2);

    const parsed = scripts.map(s => JSON.parse(s.textContent ?? "{}"));
    const types = parsed.map(p => p["@type"]);
    expect(types).toContain("SoftwareApplication");
    expect(types).toContain("FAQPage");

    const software = parsed.find(p => p["@type"] === "SoftwareApplication");
    expect(software.author).toBeDefined();
    expect(software.publisher).toBeDefined();
  });

  it("FAQPage excludes the deleted payment FAQs", async () => {
    const ui = await LandingPage();
    const { container } = render(ui as React.ReactElement);
    const scripts = Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    );
    const faq = scripts
      .map(s => JSON.parse(s.textContent ?? "{}"))
      .find(p => p["@type"] === "FAQPage");
    const text = JSON.stringify(faq);
    expect(text.toLowerCase()).not.toContain("payment methods");
    expect(text.toLowerCase()).not.toContain("free plan");
  });
});
```

- [ ] **Step 2: Run**

```bash
docker compose exec frontend npm test -- tests/landing-jsonld-faqpage.test.tsx
```
Expected: FAIL — only one JSON-LD block exists (SoftwareApplication), and it lacks author/publisher.

---

### Task C.14: Extend landing JSON-LD in `page.tsx`

**Files:**
- Modify: `frontend/app/page.tsx`

- [ ] **Step 1: Locate the existing `application/ld+json` script tag**

```bash
grep -n "application/ld+json\|SoftwareApplication" /Users/flamarion/src/tbd/frontend/app/page.tsx
```

- [ ] **Step 2: Add `author` + `publisher` to the SoftwareApplication block**

In the JSON object passed to the existing script tag, add:

```typescript
author: { "@type": "Organization", name: siteName, url: siteUrl },
publisher: { "@type": "Organization", name: siteName, url: siteUrl },
```

Use the existing `siteName` / `siteUrl` imports — don't hardcode.

- [ ] **Step 3: Build the FAQ data shared with both `Faq.tsx` and the JSON-LD block**

If the FAQ entries live solely inside `Faq.tsx`, extract them into `frontend/components/landing/faqData.ts` (or wherever existing landing data co-locates):

```typescript
// frontend/components/landing/faqData.ts
export type FaqEntry = { q: string; a: string };

export const faqEntries: ReadonlyArray<FaqEntry> = [
  // … paste the surviving entries from Faq.tsx (post PR B) here
];
```

Update `Faq.tsx` to import from this shared file. Then `page.tsx` imports the same array and renders the JSON-LD.

If extraction is too invasive, an acceptable v1 alternative is to manually duplicate the entries in `page.tsx` — but flag this as a small follow-up. (Recommendation: do the extraction; it's a 10-minute task and avoids drift.)

- [ ] **Step 4: Add a second JSON-LD script for FAQPage**

In the same head block as the existing SoftwareApplication script, render:

```tsx
<script
  type="application/ld+json"
  nonce={nonce ?? undefined}
  dangerouslySetInnerHTML={{
    __html: JSON.stringify({
      "@context": "https://schema.org",
      "@type": "FAQPage",
      mainEntity: faqEntries.map(entry => ({
        "@type": "Question",
        name: entry.q,
        acceptedAnswer: {
          "@type": "Answer",
          text: entry.a,
        },
      })),
    }),
  }}
/>
```

Use the same nonce handling pattern as the existing JSON-LD script (so CSP is satisfied).

- [ ] **Step 5: Re-run**

```bash
docker compose exec frontend npm test -- tests/landing-jsonld-faqpage.test.tsx
```
Expected: both tests PASS.

---

### Task C.15: Write the SEO admin-UI backlog file

**Files:**
- Create: `specs/seo-admin-config-backlog.md`

- [ ] **Step 1: Create the file**

Write the following content verbatim:

```markdown
# SEO admin config UI — backlog

**Created:** 2026-05-29 (split-off from the baseline-SEO spec because the operator wants this in the future but not in the baseline PR).
**Parent spec:** `specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md` §2.

## Intent

Today the per-route `title` / `description` / OG / `robots` is baked into each `page.tsx` via Next.js's `export const metadata`. The operator wants to be able to tune SEO without a code deploy — for example, changing the landing title for an A/B test, refreshing keyword-rich descriptions per route, or pointing a route at a different OG image.

## Sketched data model

New table `seo_overrides`:

| col | type | notes |
|---|---|---|
| `id` | int PK | |
| `route` | varchar(255) UNIQUE | `/`, `/login`, etc. Match Next.js's pathname. |
| `title` | varchar(255) NULL | overrides metadata.title.absolute |
| `description` | varchar(255) NULL | |
| `og_image_url` | varchar(512) NULL | absolute URL or path under `/og/` |
| `robots_index` | tinyint NULL | NULL = inherit; 0/1 = override |
| `keywords` | text NULL | comma-separated, for ops reference only (no impact on SERPs) |
| `created_by_user_id`, `updated_by_user_id`, `created_at`, `updated_at` | audit columns | |

## Plumbing

- Wrap `generateMetadata` on each public route to merge with `SeoOverride.find_by_route(pathname)`. Override values win; nulls fall through to the hardcoded metadata.
- Cache lookups for ~60 seconds (Next.js `revalidate`) to avoid hitting DB per request.
- Admin UI under `/system/seo` (superadmin-only): table of all routes, edit modal, OG image preview, "publish" button (just writes the row; revalidation kicks in on next render).

## Out of scope when picked up

- A/B testing infrastructure (drives operator policy, not data model).
- Per-locale overrides (single-language site for now).
- `hreflang` automation.
- Indexability scheduling / TTLs (just edit-now-or-later).

## Why it's not v1

- Adds DB write surface + cache invalidation + audit logging — non-trivial for an operator who hasn't validated demand for routine SEO edits.
- Per-route metadata in `page.tsx` is fine for the first 90 days of marketing — content changes will trigger code deploys anyway as the landing copy evolves.
```

- [ ] **Step 2: Stage it**

```bash
git add specs/seo-admin-config-backlog.md
```

---

### Task C.16: Commit Tasks C.13–C.15

```bash
git add frontend/app/page.tsx frontend/components/landing/Faq.tsx \
  frontend/components/landing/faqData.ts \
  frontend/tests/landing-jsonld-faqpage.test.tsx \
  specs/seo-admin-config-backlog.md
git commit -m "feat(seo): JSON-LD FAQPage + SoftwareApplication author/publisher; backlog SEO admin"
```

(If you skipped the `faqData.ts` extraction in Task C.14 Step 3, omit it from `git add`.)

---

### Task C.17: Final integration test — robots.txt + sitemap.xml served correctly

**Files:** none new; this is a fetch-based smoke test through the local stack.

- [ ] **Step 1: Boot the stack if it isn't running**

```bash
./pfv status
# If down: ./pfv start
```

- [ ] **Step 2: Fetch and inspect**

```bash
curl -s http://localhost/robots.txt | head -20
curl -s http://localhost/sitemap.xml | head -40
```
Expected:
- `robots.txt` allows `/`, `/login`, `/register`, `/privacy`, `/terms`, `/forgot-password`; disallows `/dashboard/*`, `/auth/*`, `/api/*`; references the sitemap URL.
- `sitemap.xml` lists 7 URLs: the original 5 plus `/docs` and `/docs/plans`.

- [ ] **Step 3: Spot-check the noindex meta on an auth-walled page**

```bash
curl -s http://localhost/login | grep -o '<meta[^>]*robots[^>]*>'
```
Expected: `<meta name="robots" content="index, follow"/>` (because `/login` opted in).

```bash
# Auth-walled page returns the login redirect, so curl /dashboard isn't useful.
# Spot-check a flow page instead:
curl -s http://localhost/forgot-password | grep -o '<meta[^>]*robots[^>]*>'
```
Expected: `<meta name="robots" content="noindex, nofollow"/>` (inherits root).

If steps 2 or 3 disagree with expectations, do NOT proceed to PR — diagnose the cascade (Next.js sometimes silently merges robots flags) and fix.

---

### Task C.18: Push and open PR

```bash
git push -u origin feat/baseline-seo-noindex-default
gh pr create --title "feat(seo): baseline SEO — noindex default + sitemap + JSON-LD FAQPage" --body "$(cat <<'EOF'
## Summary
- Flips the root layout's default `robots` to `{ index: false, follow: false }` and opts the 7 indexable public routes back in (`/`, `/login`, `/register`, `/privacy`, `/terms`, `/docs`, `/docs/plans`).
- Expands `sitemap.ts` to include `/docs` and `/docs/plans`.
- Adds a JSON-LD `FAQPage` block to the landing page and enriches `SoftwareApplication` with `author` / `publisher`. FAQ data extracted to a shared module so the in-page FAQ render and the structured data can't drift.
- Audits Hero `<h1>` and per-page meta distinctness.
- Adds `specs/seo-admin-config-backlog.md` as the next step toward a runtime SEO admin UI.

## Spec
specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md §2
EOF
)"
```

---

## Self-review (writing-plans skill checklist)

**1. Spec coverage** — every spec requirement maps to a task:

| Spec section | Tasks |
|---|---|
| §1 landing hardcode (PricingPreview, FAQs, "(Pro tier)", Hero notice) | B.3, B.4, B.5 |
| §1 admin nav flag-gate | B.7, B.8 |
| §1 trial-email backend gate | B.10, B.11 |
| §2 root layout default → noindex | C.2, C.3 |
| §2 7 indexable opt-ins | C.4, C.5 |
| §2 flow-only inherit noindex | C.6 (test only — no code change needed because we flipped the default) |
| §2 sitemap expansion | C.8, C.9 |
| §2 Hero `<h1>` audit | C.10, C.11 |
| §2 per-page meta distinctness | C.12 |
| §2 JSON-LD enhancement | C.13, C.14 |
| §2 backlog file | C.15 |
| §3 split helpers + provider-conditional check | A.4 |
| §3 test matrix (9 cases) | A.3 (includes both IPv4 + IPv6 loopback per spec amendment) |
| Spec + plan commits | A.2 |

No spec section is uncovered.

**2. Placeholder scan** — searched for `TBD`, `TODO`, `fill in`, `as appropriate`, `etc.`, vague verbs:
- "fill in" appears once in B.7 referring to the `AuthContextValue` fields the implementer must paste from the actual type — this is mechanical (open the type, copy fields) not a design decision. Acceptable.
- No "TODO", no "implement later", no "see above". Each task shows the actual code.

**3. Type consistency** —
- `_reject_metadata_or_unsafe` and `_reject_private_or_loopback` names match between the spec, the test file references, and the implementation file references.
- `app_settings` import alias is consistent across the gate test and the implementation.
- `billingUiEnabled` (camelCase) on the frontend, `billing_ui_enabled` (snake) on the backend — matches the established convention from the 2026-05-21 spec.
- `faqEntries` / `FaqEntry` names used in C.14 Step 3 are consistently referenced in C.14 Step 4 and C.16's git add.

No drift found.

---

## Execution handoff

**Plan complete and saved to `specs/2026-05-29-hide-payments-seo-baseline-ollama-lan-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks; matches the user's [[feedback_subagent_driven]] + [[feedback_subagent_execution_guardrails]] rules.

**2. Inline Execution** — execute tasks in this session with batch checkpoints.

Per the user's earlier choice ("Brainstorm → plan → stop"), the recommended next step is **STOP here for plan review** before either execution mode kicks off.
