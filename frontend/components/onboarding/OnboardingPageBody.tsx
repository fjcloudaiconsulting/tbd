"use client";

/**
 * OnboardingPageBody — the first-run wizard (L3.3).
 *
 * Steps (full sequence for org owners):
 *   1. Welcome      — brand-aligned intro, one CTA.
 *   2. First account — minimal form (name + type), submitted to
 *                       POST /api/v1/accounts. Skippable.
 *   3. Demo seed     — yes/no opt-in for the L3.3 demo dataset.
 *                       On yes, POST /api/v1/users/me/onboarding/seed-demo.
 *                       On 409 we surface a soft warning (never 500).
 *   4. Tour offer    — yes/no for the dashboard tour, which starts
 *                       on the dashboard after the wizard finishes.
 *
 * Role-aware step list: the demo-seed step is owner-only because the
 * underlying endpoint is guarded by `require_org_owner` (added in
 * 78d6409). Invited admin / member users skip straight from the
 * account step to the tour offer, so they never see an affordance
 * they cannot use.
 *
 * Each step has a Skip button that advances without firing the
 * step's side effect. The final step (or the user clicking Skip on
 * step 4) always calls POST /onboarding/complete and redirects to
 * /dashboard. Calling complete on Skip is intentional — we never
 * want the wizard to greet the user a second time just because they
 * dismissed it once.
 *
 * Idempotency guard: if `user.onboarded_at` is already set we
 * redirect to /dashboard on mount. This keeps the route safe to
 * bookmark and protects users who navigate back to /onboarding.
 */
import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/components/auth/AuthProvider";
import { useTour } from "@/components/tour/useTour";
import { apiFetch } from "@/lib/api";
import { btnPrimary, btnSecondary, card, input, label } from "@/lib/styles";
import type { AccountType } from "@/lib/types";

import {
  DASHBOARD_TOUR_STEPS,
  TOUR_FLAG_KEY,
  TOUR_FLAG_VALUE_DASHBOARD,
} from "@/lib/help/tour";

// sessionStorage flag the Google SSO callback page sets when a new
// local user has just been created. Reading it here inserts a
// privacy disclosure step at the front of the wizard. Kept in sync
// with `SSO_DISCLOSURE_PENDING_KEY` in
// frontend/app/auth/google/callback/page.tsx — drift between the
// writer + reader would silently disable the disclosure for every
// new SSO user.
const SSO_DISCLOSURE_PENDING_KEY = "tbd-sso-disclosure-pending";

type Step = "sso-disclosure" | "welcome" | "account" | "demo" | "tour";

const FULL_STEP_ORDER: Step[] = ["welcome", "account", "demo", "tour"];
const NON_OWNER_STEP_ORDER: Step[] = ["welcome", "account", "tour"];

export default function OnboardingPageBody() {
  const { user, loading, refreshMe } = useAuth();
  const router = useRouter();
  const tour = useTour();

  // First-run SSO disclosure flag — set by the Google callback page
  // when it just created a new local user. Read synchronously from
  // sessionStorage on initial render so the wizard never paints a
  // mismatched first step. Falls back to false in non-browser
  // contexts (SSR, test envs without sessionStorage).
  const [showSsoDisclosure, setShowSsoDisclosure] = useState<boolean>(() => {
    try {
      if (typeof window === "undefined") return false;
      return window.sessionStorage.getItem(SSO_DISCLOSURE_PENDING_KEY) === "1";
    } catch {
      return false;
    }
  });

  // Owners get the full four-step wizard; admins / members skip the
  // demo-seed step because the seed endpoint is owner-only (78d6409).
  // Falling back to NON_OWNER_STEP_ORDER while the user is still
  // loading is safe — the loading branch below returns the spinner
  // before any step renders, so this value is only consumed once
  // `user` is populated.
  const BASE_STEP_ORDER: Step[] =
    user?.role === "owner" ? FULL_STEP_ORDER : NON_OWNER_STEP_ORDER;
  const STEP_ORDER: Step[] = showSsoDisclosure
    ? (["sso-disclosure", ...BASE_STEP_ORDER] as Step[])
    : BASE_STEP_ORDER;

  const [step, setStep] = useState<Step>(
    showSsoDisclosure ? "sso-disclosure" : "welcome",
  );
  const [accountName, setAccountName] = useState("Main Checking");
  const [accountTypes, setAccountTypes] = useState<AccountType[]>([]);
  const [selectedTypeId, setSelectedTypeId] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [seedNote, setSeedNote] = useState<string | null>(null);

  // Redirect away when the user is already onboarded.
  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (user.onboarded_at) {
      router.replace("/dashboard");
    }
  }, [user, loading, router]);

  // Lazy-load account types when entering the account step.
  useEffect(() => {
    if (step !== "account") return;
    if (accountTypes.length) return;
    apiFetch<AccountType[]>("/api/v1/account-types")
      .then((rows) => {
        setAccountTypes(rows);
        const checking =
          rows.find((r) => r.slug === "checking") ?? rows[0] ?? null;
        if (checking) setSelectedTypeId(checking.id);
      })
      .catch(() => {
        // Non-fatal — user can still skip the step.
        setAccountTypes([]);
      });
  }, [step, accountTypes.length]);

  function goNext() {
    const idx = STEP_ORDER.indexOf(step);
    if (idx < 0 || idx === STEP_ORDER.length - 1) return;
    setStep(STEP_ORDER[idx + 1]);
    setError(null);
  }

  function acceptSsoDisclosure() {
    // Clear the sessionStorage flag so the disclosure does not show
    // again, then advance into the standard onboarding wizard.
    try {
      window.sessionStorage.removeItem(SSO_DISCLOSURE_PENDING_KEY);
    } catch {
      // sessionStorage unavailable — already past it, nothing to do.
    }
    setShowSsoDisclosure(false);
    setStep("welcome");
    setError(null);
  }

  async function finishWizard(startTour: boolean) {
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch("/api/v1/users/me/onboarding/complete", {
        method: "POST",
      });
      await refreshMe();
      if (startTour) {
        // The dashboard is not yet mounted, so we cannot call
        // tour.start() from here directly. Stash a flag in
        // sessionStorage and the dashboard reads it on mount.
        try {
          window.sessionStorage.setItem(TOUR_FLAG_KEY, TOUR_FLAG_VALUE_DASHBOARD);
        } catch {
          // sessionStorage may be unavailable in private mode. The
          // tour just will not start automatically — non-fatal.
        }
      }
      router.replace("/dashboard");
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Could not finish onboarding. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCreateAccount() {
    if (!selectedTypeId) {
      goNext();
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch("/api/v1/accounts", {
        method: "POST",
        body: JSON.stringify({
          name: accountName.trim() || "Main Checking",
          account_type_id: selectedTypeId,
          currency: "EUR",
          opening_balance: "0.00",
        }),
      });
      goNext();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Could not create the account. You can skip this step.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSeedYes() {
    setSubmitting(true);
    setSeedNote(null);
    setError(null);
    try {
      await apiFetch("/api/v1/users/me/onboarding/seed-demo", {
        method: "POST",
      });
      setSeedNote("Sample data ready. We are heading to your dashboard.");
      goNext();
    } catch (err) {
      // 409 from the backend is expected when the org already has
      // data. Surface a non-blocking note and let the user advance
      // themselves rather than yanking them off the step before they
      // read why we declined.
      const message = err instanceof Error ? err.message : String(err);
      if (message.includes("org_has_data") || message.includes("409")) {
        setSeedNote(
          "Your account already has data, so we skipped the sample set.",
        );
      } else {
        setError(
          "We could not load the sample data. You can still continue.",
        );
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (loading || !user || user.onboarded_at) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg">
        <div
          role="status"
          aria-label="Loading onboarding"
          className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent"
        />
      </div>
    );
  }

  const stepIdx = STEP_ORDER.indexOf(step);

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4 py-8">
      <div
        className={`${card} w-full max-w-xl p-8`}
        data-testid="onboarding-card"
      >
        <div className="mb-6 flex items-center justify-between">
          <div className="text-xs uppercase tracking-[0.08em] text-text-muted">
            {step === "sso-disclosure" ? "Privacy" : "Welcome"}
          </div>
          <div className="text-xs text-text-muted">
            Step {stepIdx + 1} of {STEP_ORDER.length}
          </div>
        </div>

        {step === "sso-disclosure" && (
          <div data-testid="onboarding-sso-disclosure">
            <h1 className="mb-3 text-2xl font-semibold text-text-primary">
              About your Google sign-in
            </h1>
            <p className="mb-4 text-sm leading-relaxed text-text-secondary">
              You signed in with Google. Before we set up your account,
              here is exactly what that means.
            </p>

            <h2 className="mb-2 text-sm font-semibold uppercase tracking-[0.06em] text-text-primary">
              What Google shares with us
            </h2>
            <p className="mb-2 text-sm leading-relaxed text-text-secondary">
              We ask Google for three pieces of information, using the
              standard scopes <span className="font-mono text-xs">openid</span>,
              {" "}<span className="font-mono text-xs">email</span>, and
              {" "}<span className="font-mono text-xs">profile</span>:
            </p>
            <ul className="mb-5 list-disc space-y-1 pl-5 text-sm leading-relaxed text-text-secondary">
              <li>Your name.</li>
              <li>
                Your email address and whether Google has verified it.
              </li>
              <li>Your profile photo, if you have one.</li>
            </ul>
            <p className="mb-5 text-sm leading-relaxed text-text-secondary">
              We use that information only to create your The Better
              Decision account and sign you in.
            </p>

            <h2 className="mb-2 text-sm font-semibold uppercase tracking-[0.06em] text-text-primary">
              What we never see
            </h2>
            <ul className="mb-5 list-disc space-y-1 pl-5 text-sm leading-relaxed text-text-secondary">
              <li>We do not get your Google password.</li>
              <li>
                We do not access Gmail, Drive, Calendar, contacts, or
                anything else in your Google account.
              </li>
              <li>
                We do not connect to your bank, card, or any financial
                account through Google.
              </li>
            </ul>

            <h2 className="mb-2 text-sm font-semibold uppercase tracking-[0.06em] text-text-primary">
              What Google never sees
            </h2>
            <p className="mb-5 text-sm leading-relaxed text-text-secondary">
              Google does not get access to the accounts, transactions,
              budgets, or any other financial information you keep
              inside The Better Decision. Your data stays here.
            </p>

            <p className="mb-6 text-xs leading-relaxed text-text-muted">
              Full details:{" "}
              <Link
                href="/privacy"
                className="underline hover:text-text-primary"
                target="_blank"
                rel="noreferrer"
              >
                Privacy Policy
              </Link>
              {" "}and{" "}
              <Link
                href="/terms"
                className="underline hover:text-text-primary"
                target="_blank"
                rel="noreferrer"
              >
                Terms of Service
              </Link>
              .
            </p>

            <div className="flex justify-end">
              <button
                type="button"
                className={btnPrimary}
                onClick={acceptSsoDisclosure}
                data-testid="onboarding-sso-disclosure-continue"
              >
                Continue
              </button>
            </div>
          </div>
        )}

        {step === "welcome" && (
          <div>
            <h1 className="mb-3 text-2xl font-semibold text-text-primary">
              Better decisions about money start here.
            </h1>
            <p className="mb-6 text-sm leading-relaxed text-text-secondary">
              The Better Decision helps you see where your money goes,
              plan for what is next, and stay on top of every billing
              period. We will walk you through the basics in under a
              minute.
            </p>
            <div className="flex justify-end gap-3">
              <button
                type="button"
                className={btnSecondary}
                onClick={() => finishWizard(false)}
                data-testid="onboarding-skip-all"
              >
                Skip for now
              </button>
              <button
                type="button"
                className={btnPrimary}
                onClick={goNext}
                data-testid="onboarding-next"
              >
                Let us begin
              </button>
            </div>
          </div>
        )}

        {step === "account" && (
          <div>
            <h1 className="mb-3 text-2xl font-semibold text-text-primary">
              Add your first account
            </h1>
            <p className="mb-6 text-sm leading-relaxed text-text-secondary">
              Start with one. You can add more later. Every transaction
              belongs to an account, and balances flow from there.
            </p>
            <div className="mb-4">
              <label className={label} htmlFor="onboarding-account-name">
                Account name
              </label>
              <input
                id="onboarding-account-name"
                className={input}
                value={accountName}
                onChange={(e) => setAccountName(e.target.value)}
                maxLength={200}
              />
            </div>
            <div className="mb-6">
              <label className={label} htmlFor="onboarding-account-type">
                Type
              </label>
              <select
                id="onboarding-account-type"
                className={input}
                value={selectedTypeId ?? ""}
                onChange={(e) => setSelectedTypeId(Number(e.target.value))}
              >
                {accountTypes.length === 0 ? (
                  <option value="">Loading types...</option>
                ) : null}
                {accountTypes.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>
            {error ? (
              <div className="mb-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                {error}
              </div>
            ) : null}
            <div className="flex justify-between gap-3">
              <button
                type="button"
                className={btnSecondary}
                onClick={goNext}
                disabled={submitting}
                data-testid="onboarding-skip"
              >
                Skip
              </button>
              <button
                type="button"
                className={btnPrimary}
                onClick={handleCreateAccount}
                disabled={submitting || !selectedTypeId}
                data-testid="onboarding-create-account"
              >
                Create account
              </button>
            </div>
          </div>
        )}

        {step === "demo" && (
          <div>
            <h1 className="mb-3 text-2xl font-semibold text-text-primary">
              Want sample data to try things out?
            </h1>
            <p className="mb-6 text-sm leading-relaxed text-text-secondary">
              We can drop in two sample accounts and a couple of
              months of fake transactions. It is the fastest way to
              see what the dashboard looks like with data. You can
              delete it anytime.
            </p>
            {seedNote ? (
              <div
                className="mb-4 rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-secondary"
                data-testid="onboarding-seed-note"
              >
                {seedNote}
              </div>
            ) : null}
            {error ? (
              <div className="mb-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                {error}
              </div>
            ) : null}
            <div className="flex justify-between gap-3">
              <button
                type="button"
                className={btnSecondary}
                onClick={goNext}
                disabled={submitting}
                data-testid="onboarding-decline-seed"
              >
                No thanks
              </button>
              <button
                type="button"
                className={btnPrimary}
                onClick={handleSeedYes}
                disabled={submitting}
                data-testid="onboarding-accept-seed"
              >
                Yes, add sample data
              </button>
            </div>
          </div>
        )}

        {step === "tour" && (
          <div>
            <h1 className="mb-3 text-2xl font-semibold text-text-primary">
              Quick tour of the dashboard?
            </h1>
            <p className="mb-6 text-sm leading-relaxed text-text-secondary">
              Five short callouts on the parts of the dashboard you
              will use most. Skippable at any point.
            </p>
            {error ? (
              <div className="mb-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                {error}
              </div>
            ) : null}
            <div className="flex justify-between gap-3">
              <button
                type="button"
                className={btnSecondary}
                onClick={() => {
                  // No tour. Still mark onboarding complete.
                  tour.reset();
                  finishWizard(false);
                }}
                disabled={submitting}
                data-testid="onboarding-decline-tour"
              >
                Maybe later
              </button>
              <button
                type="button"
                className={btnPrimary}
                onClick={() => {
                  // The actual start happens on the dashboard via the
                  // sessionStorage flag. We do not call tour.start
                  // here because the anchor DOM is not yet mounted.
                  void DASHBOARD_TOUR_STEPS;
                  finishWizard(true);
                }}
                disabled={submitting}
                data-testid="onboarding-accept-tour"
              >
                Yes, show me
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

