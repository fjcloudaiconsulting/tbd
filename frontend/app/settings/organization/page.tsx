"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { mutate } from "swr";
import SettingsLayout from "@/components/SettingsLayout";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isOpenPeriod } from "@/lib/billingPeriodStatus";
import {
  mapBillingCycleError,
  mapBillingPeriodCloseError,
  validateBillingCycleDay,
} from "@/lib/formErrors";
import { projectedPeriodEnd } from "@/lib/format";
import { isAdmin } from "@/lib/auth";
import DemoDataCard from "@/components/settings/DemoDataCard";
import SchedulerSettingsCard from "@/components/settings/SchedulerSettingsCard";
import MembersSection from "@/components/settings/MembersSection";
import SmartRulesSection from "@/components/settings/SmartRulesSection";
import {
  input,
  label,
  btnLink,
  btnPrimary,
  btnSecondary,
  btnDangerSolid,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  success as successCls,
} from "@/lib/styles";
import type { OrgSetting } from "@/lib/types";

export default function OrganizationSettingsPage() {
  const { user, loading, refreshMe } = useAuth();
  const router = useRouter();

  const [settings, setSettings] = useState<OrgSetting[]>([]);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    message: string;
    variant: "warning" | "danger";
    action: () => void;
  } | null>(null);
  // Initial value falls back to AuthContext.user.billing_cycle_day so a slow or
  // failed GET /billing-cycle never leaves the field unusably blank.
  const [billingCycleDay, setBillingCycleDay] = useState<string>(
    user?.billing_cycle_day != null ? String(user.billing_cycle_day) : ""
  );
  // True once the admin has typed; the mount-time GET response is dropped if
  // this is set, so a slow response can't overwrite an in-progress edit or
  // resurrect a stale value right after a fast Save.
  const userEditedCycleDayRef = useRef(false);
  const [savingCycle, setSavingCycle] = useState(false);
  // Inline error for the billing-cycle field. Surfaced under the input
  // (not the page-level error banner) so the admin sees exactly which
  // field needs fixing.
  const [cycleFieldError, setCycleFieldError] = useState<string | null>(null);
  // Cached server-confirmed value used to disable Save when nothing
  // changed. Updated after a successful save or a successful GET.
  const [savedCycleDay, setSavedCycleDay] = useState<string>(
    user?.billing_cycle_day != null ? String(user.billing_cycle_day) : ""
  );
  const [currentPeriod, setCurrentPeriod] = useState<{
    id: number;
    start_date: string;
    end_date: string | null;
  } | null>(null);
  const [closingPeriod, setClosingPeriod] = useState(false);

  // L3.1 Danger Zone — owner-only data reset.
  // Compare user.role exactly; do NOT use isOwner() from @/lib/auth,
  // which treats is_superadmin as owner — the backend rejects
  // superadmin tenant bypass, so the UI must mirror that.
  const [resetPhrase, setResetPhrase] = useState("");
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState("");

  // Track D — owner-only org rename. Read-only display for non-owners.
  // Same superadmin caveat as the Danger Zone above: the backend's
  // require_org_owner gate refuses the platform-superadmin bypass on
  // tenant routes, so the UI mirrors that strictness exactly.
  const [renaming, setRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState("");
  const [renameSaving, setRenameSaving] = useState(false);
  const [renameError, setRenameError] = useState("");

  // Track E — manual balance adjustment toggle (admin-only). Local state
  // mirrors the user.allow_manual_balance_adjustment value pulled from
  // GET /me on mount; flips optimistically through the confirm dialog.
  const [allowAdjustEnabled, setAllowAdjustEnabled] = useState<boolean>(
    user?.allow_manual_balance_adjustment ?? false
  );
  const [adjustSaving, setAdjustSaving] = useState(false);
  const [pendingToggleTo, setPendingToggleTo] = useState<boolean | null>(null);

  const admin = user ? isAdmin(user) : false;
  const isOrgOwner = user?.role === "owner";
  const orgName = user?.org_name ?? "";
  const expectedResetPhrase = `RESET ${orgName}`;
  const resetPhraseMatches = resetPhrase.trim() === expectedResetPhrase;

  // Derived from `savedCycleDay`, NOT the live input. Typing a new cycle day
  // changes nothing on the server, so the "Current:" line must not react to
  // keystrokes: it would re-date the current period on screen while the
  // preview under the input says saving does not re-date it. Two
  // contradictory statements in one card.
  //
  // Known wrong for off-grid orgs and NOT fixed here: `projectedPeriodEnd`
  // assumes the open period starts on the cycle day, so for an org whose
  // open period sits off the grid (any org that ever closed manually) the
  // projection can be a month out. Correcting it needs the backend to return
  // the period's projected end, which is TBD-235.
  const currentPeriodEndDisplay = currentPeriod
    ? currentPeriod.end_date ?? projectedPeriodEnd(currentPeriod.start_date, Number(savedCycleDay))
    : null;

  useEffect(() => {
    if (!loading && !admin) router.replace("/settings");
  }, [loading, admin, router]);

  // AuthProvider hydrates `user` asynchronously, so the state initializer
  // typically locks the field at "" before user.billing_cycle_day exists.
  // Once it lands, seed the field — but only if the admin hasn't started
  // editing and an authoritative GET response hasn't already filled the
  // field. This is the failed-GET fallback the initializer alone can't
  // provide.
  useEffect(() => {
    if (user?.billing_cycle_day == null) return;
    if (userEditedCycleDayRef.current) return;
    setBillingCycleDay((current) =>
      current === "" ? String(user.billing_cycle_day) : current
    );
  }, [user?.billing_cycle_day]);

  // Track E: keep the local toggle state in sync with the AuthContext
  // user. Once /me lands the value flips from the SSR default (false)
  // to whatever the org actually has.
  useEffect(() => {
    if (user?.allow_manual_balance_adjustment !== undefined) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- sync the local allow-adjust toggle to the org value once AuthContext user (/me) lands
      setAllowAdjustEnabled(user.allow_manual_balance_adjustment);
    }
  }, [user?.allow_manual_balance_adjustment]);

  async function applyAllowAdjustToggle(next: boolean) {
    setAdjustSaving(true);
    setError("");
    try {
      await apiFetch("/api/v1/settings/manual-balance-adjustment", {
        method: "PUT",
        body: JSON.stringify({ enabled: next }),
      });
      setAllowAdjustEnabled(next);
      // Pull the fresh user shape so AuthContext.user.allow_manual_balance_adjustment
      // updates everywhere that gates on it (the accounts page button).
      await refreshMe();
      setSuccessMsg(
        next
          ? "Manual balance adjustment enabled"
          : "Manual balance adjustment disabled"
      );
      setTimeout(() => setSuccessMsg(""), 3000);
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to update setting"));
    } finally {
      setAdjustSaving(false);
    }
  }

  function handleToggleAllowAdjust(next: boolean) {
    setPendingToggleTo(next);
  }

  const reload = useCallback(async () => {
    try {
      const data = await apiFetch<OrgSetting[]>("/api/v1/settings");
      setSettings(data);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    if (admin) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- initial admin data fetch: reload() + the billing-period/cycle fetches write org settings into state
      reload();
      apiFetch<{ id: number; start_date: string; end_date: string | null }>(
        "/api/v1/settings/billing-period"
      ).then(setCurrentPeriod).catch(() => {});
      apiFetch<{ billing_cycle_day: number }>("/api/v1/settings/billing-cycle")
        .then((r) => {
          const next = String(r.billing_cycle_day);
          setSavedCycleDay(next);
          if (!userEditedCycleDayRef.current) {
            setBillingCycleDay(next);
          }
        })
        .catch(() => {
          // Swallow: the AuthContext-seeding effect above leaves a usable
          // (possibly stale) value in place when GET fails. If user is also
          // unavailable, the field stays empty and client-side validation
          // will guide the admin on Save.
        });
    }
  }, [admin, reload]);

  function startRename() {
    setRenameDraft(orgName);
    setRenameError("");
    setRenaming(true);
  }

  function cancelRename() {
    setRenaming(false);
    setRenameDraft("");
    setRenameError("");
  }

  async function handleSaveRename(e: FormEvent) {
    e.preventDefault();
    if (!user) return;
    const trimmed = renameDraft.trim();
    // Client-side guards mirror the server: empty/whitespace-only
    // is rejected here so we don't flash a spinner only to see a 422.
    // Same-name submissions also short-circuit; the server treats
    // them as a no-op but there's no point in the round trip.
    if (trimmed === "") {
      setRenameError("Name cannot be empty");
      return;
    }
    if (trimmed === orgName) {
      cancelRename();
      return;
    }
    setRenameSaving(true);
    setRenameError("");
    try {
      await apiFetch(`/api/v1/orgs/${user.org_id}/rename`, {
        method: "PATCH",
        body: JSON.stringify({ name: trimmed }),
      });
      // Pull the fresh user shape so org_name updates everywhere
      // that reads from AuthContext (sidebar, header, etc.).
      await refreshMe();
      setRenaming(false);
      setRenameDraft("");
      setSuccessMsg("Organization renamed");
      setTimeout(() => setSuccessMsg(""), 3000);
    } catch (err) {
      setRenameError(extractErrorMessage(err, "Failed to rename organization"));
    } finally {
      setRenameSaving(false);
    }
  }

  async function handleSaveCycle(e: FormEvent) {
    e.preventDefault();
    if (savingCycle) return;
    setError("");
    const fieldErr = validateBillingCycleDay(billingCycleDay);
    if (fieldErr) {
      setCycleFieldError(fieldErr);
      return;
    }
    setCycleFieldError(null);
    const day = Number(billingCycleDay);
    setSavingCycle(true);
    try {
      await apiFetch("/api/v1/settings/billing-cycle", {
        method: "PUT",
        body: JSON.stringify({ billing_cycle_day: day }),
      });
      // Server now matches local state. Clear the dirty flag so a future
      // GET (e.g., on revisit) can re-sync without being treated as a
      // stale overwrite, and update the cached server value so the Save
      // button correctly disables again until the next edit.
      userEditedCycleDayRef.current = false;
      setSavedCycleDay(String(day));
      // No GET /billing-period refetch here on purpose. The PUT writes one
      // org column and leaves the open period alone, so there is nothing to
      // re-read, and a failing refetch used to land in the catch below and
      // report a save failure for a save that had already succeeded.
      setSuccessMsg(
        `Billing cycle saved. Day ${day} applies from your next billing period. ` +
          "Closing a period by hand still opens the next one the day after the close date."
      );
      setTimeout(() => setSuccessMsg(""), 4000);
    } catch (err) {
      // Map known status codes to friendly copy; the raw server message
      // is only kept when it is already a safe sentence.
      setError(mapBillingCycleError(err));
      // Leave the form filled with the admin's input so they can correct
      // it without re-typing.
    } finally {
      setSavingCycle(false);
    }
  }

  function handleClosePeriod() {
    setConfirmAction({
      title: "Close billing period",
      // We POST /billing-period/close with no `close_date`, so the service
      // picks the date (see `close_period` in `billing_service.py`).
      //
      // TBD-241 D6: it is NOT always yesterday. The service clamps the close to
      // the first period boundary that already exists after the open one, so on
      // a lapsed org with stubs ahead of it the period ends well before
      // yesterday and the row that opens is an existing stub, not a new row
      // starting today. The copy below therefore states the RULE and never
      // names a date it cannot know before the call. The success toast needs no
      // such care: it renders `p.start_date` from a refetch, so it already
      // reports the real new start whatever the clamp did. There is still no
      // un-close endpoint or UI, so the caution stays until TBD-233/TBD-235.
      //
      // The sentence below states only the guarantee that ALWAYS holds. An
      // earlier draft promised "if a later period already exists, the close
      // stops at the day before it starts", which is false whenever that later
      // period starts after the close date: nothing stops it, and nothing needs
      // to. Code review F6.
      message:
        `Close the current billing period starting ${currentPeriod?.start_date}? ` +
        "This ends the period and opens the next one. A period you have already planned is never swallowed, because the close never reaches past one. " +
        "There is no way to reopen a period from the app yet, so close only when the period is done.",
      variant: "warning",
      action: async () => {
        if (closingPeriod) return;
        setClosingPeriod(true);
        try {
          await apiFetch("/api/v1/settings/billing-period/close", { method: "POST" });
          const p = await apiFetch<{ id: number; start_date: string; end_date: string | null }>(
            "/api/v1/settings/billing-period"
          );
          setCurrentPeriod(p);
          setSuccessMsg(
            p?.start_date
              ? `Previous period closed. New period opened on ${p.start_date}.`
              : "Previous period closed. A new period is now open.",
          );
          setTimeout(() => setSuccessMsg(""), 4000);
        } catch (err) {
          setError(mapBillingPeriodCloseError(err));
        } finally {
          setClosingPeriod(false);
        }
      },
    });
  }

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({ key, value }),
      });
      setKey("");
      setValue("");
      await reload();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleUpdate(settingKey: string) {
    setError("");
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({ key: settingKey, value: editingValue }),
      });
      setEditingKey(null);
      await reload();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleDelete(settingKey: string) {
    setConfirmAction({
      title: "Delete Setting",
      message: `Delete setting "${settingKey}"?`,
      variant: "danger",
      action: async () => {
        setError("");
        try {
          await apiFetch(`/api/v1/settings/${encodeURIComponent(settingKey)}`, {
            method: "DELETE",
          });
          await reload();
        } catch (err) {
          setError(extractErrorMessage(err));
        }
      },
    });
  }

  if (loading || !user || !admin) {
    return (
      <SettingsLayout activeTab="/settings/organization">
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      </SettingsLayout>
    );
  }

  return (
    <SettingsLayout activeTab="/settings/organization">
      {error && (
        <p role="alert" aria-live="polite" className={errorCls}>
          {error}
        </p>
      )}
      {successMsg && (
        <p role="status" aria-live="polite" className={successCls}>
          {successMsg}
        </p>
      )}

      <div className="space-y-6">
        {/* Organization Name */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Organization</h2>
          </div>
          <div className="p-6">
            {isOrgOwner ? (
              renaming ? (
                <form onSubmit={handleSaveRename} className="space-y-3">
                  <label className={label} htmlFor="org-rename-input">
                    New organization name
                  </label>
                  <input
                    id="org-rename-input"
                    type="text"
                    className={input}
                    value={renameDraft}
                    onChange={(e) => setRenameDraft(e.target.value)}
                    maxLength={80}
                    autoFocus
                    aria-invalid={renameError ? true : undefined}
                    aria-describedby={renameError ? "org-rename-error" : undefined}
                  />
                  {renameError && (
                    <p id="org-rename-error" className={errorCls}>
                      {renameError}
                    </p>
                  )}
                  <div className="flex gap-2">
                    <button
                      type="submit"
                      disabled={
                        renameSaving ||
                        renameDraft.trim() === "" ||
                        renameDraft.trim() === orgName
                      }
                      className={btnPrimary}
                      aria-label="Save organization name"
                    >
                      {renameSaving ? (
                        <span className="inline-flex items-center gap-2">
                          <Loader2 className="h-4 w-4 animate-spin" />
                          Saving...
                        </span>
                      ) : (
                        "Save"
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={cancelRename}
                      disabled={renameSaving}
                      className={btnSecondary}
                      aria-label="Cancel organization rename"
                    >
                      Cancel
                    </button>
                  </div>
                </form>
              ) : (
                <div className="flex items-center justify-between gap-4">
                  <p className="text-sm text-text-secondary">{user.org_name}</p>
                  <button
                    type="button"
                    onClick={startRename}
                    className={btnSecondary}
                  >
                    Rename
                  </button>
                </div>
              )
            ) : (
              <p className="text-sm text-text-secondary">{user.org_name}</p>
            )}
          </div>
        </div>

        {/* Billing Period */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Billing period</h2>
          </div>
          <div className="p-6 space-y-4">
            {currentPeriod && (
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm text-text-primary">
                    Current: {currentPeriod.start_date}
                    {currentPeriodEndDisplay ? `, ${currentPeriodEndDisplay}` : ", open"}
                  </p>
                  {/*
                    The closed branch is unreachable, and no planned ticket
                    makes it reachable. `currentPeriod` is only ever filled
                    from GET /billing-period, which returns
                    `billing_service.get_current_period` and by construction
                    only yields rows with `end_date IS NULL`. TBD-234's
                    period roster is a SEPARATE read-only route with its own
                    state; it never feeds this variable. The copy is kept
                    correct anyway because the branch is one line away from
                    being wrong again: nothing in the backend locks a closed
                    period, and transactions are bucketed by the date they
                    settled, so the old "locked" claim was false.
                    Not covered by a test: asserting it needs a fixture the
                    real API cannot produce.
                  */}
                  <p className="text-xs text-text-muted">
                    {!isOpenPeriod(currentPeriod)
                      ? "Closed. This period's window is fixed at these dates. Transactions are still counted by the date they settled, so editing a transaction can move it in or out."
                      : "Open. New transactions are being recorded in this period."}
                  </p>
                </div>
                {isOpenPeriod(currentPeriod) && (
                  <button
                    type="button"
                    onClick={handleClosePeriod}
                    disabled={closingPeriod}
                    aria-busy={closingPeriod}
                    className={`${btnPrimary} w-full sm:w-auto sm:min-h-0 inline-flex items-center justify-center gap-2`}
                  >
                    {closingPeriod && (
                      <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                    )}
                    {closingPeriod ? "Closing..." : "Close period"}
                  </button>
                )}
              </div>
            )}

            <form
              onSubmit={handleSaveCycle}
              className="flex flex-col gap-3 sm:flex-row sm:items-start sm:gap-3"
              aria-busy={savingCycle}
            >
              <div className="flex-1">
                <label htmlFor="billing-cycle-day" className={label}>
                  Billing cycle day
                </label>
                <input
                  id="billing-cycle-day"
                  type="number"
                  min={1}
                  max={28}
                  inputMode="numeric"
                  value={billingCycleDay}
                  onChange={(e) => {
                    userEditedCycleDayRef.current = true;
                    setBillingCycleDay(e.target.value);
                    // Re-validate live so the error clears the moment the
                    // value becomes valid (and surfaces on the first
                    // out-of-range keystroke).
                    setCycleFieldError(validateBillingCycleDay(e.target.value));
                  }}
                  className={`${input} w-full sm:w-24`}
                  aria-describedby={
                    cycleFieldError
                      ? "billing-cycle-day-err billing-cycle-day-hint billing-cycle-day-preview"
                      : "billing-cycle-day-hint billing-cycle-day-preview"
                  }
                  aria-invalid={cycleFieldError ? true : undefined}
                />
                <p id="billing-cycle-day-hint" className="mt-1.5 text-xs text-text-muted">
                  Day of the month each new period starts. Days 1 to 28 only, so every month has it.
                </p>
                {/*
                  Dirty + valid + changed preview. Tells the admin WHEN the
                  new cycle day takes effect, so the consequence of changing
                  it is visible before they commit. Static placeholder keeps
                  layout stable when no preview is shown.

                  DELIBERATELY DATELESS — do not "helpfully" compute the
                  destination date here. Saving writes only the org's cycle
                  day; the roster changes at the next close, whose date the
                  browser cannot know. The backend decides boundaries off
                  server-local `date.today()` (UTC in this deployment) while
                  the browser can only see the viewer's local date, and the
                  cycle-day rule is a THRESHOLD (`today.day >= cycle_day`
                  picks this month, else last month), not a linear offset, so
                  a single day of local-vs-server skew flips the answer by a
                  FULL MONTH. An admin in Europe/Amsterdam at 01:00 on the
                  15th would be shown one month while the server, still on
                  the 14th in UTC, is working in another. The client cannot
                  know the server's date without a new API field, so we state
                  the rule instead of guessing a date.
                */}
                <p
                  id="billing-cycle-day-preview"
                  className="mt-1.5 min-h-[1rem] text-xs text-text-muted"
                  aria-live="polite"
                >
                  {(() => {
                    if (cycleFieldError) return "";
                    if (billingCycleDay.trim() === "") return "";
                    if (billingCycleDay === savedCycleDay) return "";
                    const day = Number(billingCycleDay);
                    if (!Number.isFinite(day)) return "";
                    // The `currentPeriod` guard is INTENTIONAL, not vestigial,
                    // and it survives TBD-239 by an explicit decision: the
                    // new sentence still makes a claim about "the period you
                    // are in now", and if GET /billing-period failed we do
                    // not know there is one. Staying silent beats reassuring
                    // an admin about a period we cannot vouch for.
                    if (!currentPeriod?.start_date) return "";
                    // Does NOT promise the current period keeps its dates.
                    // `BillingCloseJob.is_due` compares the open period's
                    // start against the window derived from the NEW cycle
                    // day, so a forward move past a day that already went by
                    // this month makes the very next scheduler tick (900s)
                    // close the current period, with an end date in the
                    // past. Any "nothing changes now" wording here would be
                    // contradicted inside fifteen minutes.
                    return "Saving changes the day your billing periods start on, from your next period onward. Saving on its own does not re-date the period you are in now. If automatic billing close is on, that period may close shortly so the new day can take effect.";
                  })()}
                </p>
                {cycleFieldError && (
                  <p
                    id="billing-cycle-day-err"
                    role="alert"
                    aria-live="polite"
                    className="mt-1.5 text-xs text-danger"
                  >
                    {cycleFieldError}
                  </p>
                )}
              </div>
              <div className="flex flex-col gap-2 sm:flex-row sm:mt-[26px]">
                <button
                  type="submit"
                  disabled={
                    savingCycle ||
                    cycleFieldError !== null ||
                    billingCycleDay.trim() === "" ||
                    billingCycleDay === savedCycleDay
                  }
                  aria-busy={savingCycle}
                  className={`${btnPrimary} w-full sm:w-auto sm:min-h-0 inline-flex items-center justify-center gap-2`}
                >
                  {savingCycle && (
                    <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  )}
                  {savingCycle ? "Saving..." : "Save"}
                </button>
                {/*
                  Discard mirrors the Forecast Plans Save/Cancel pattern.
                  Hidden until the field is dirty so it doesn't add noise
                  to the resting state.
                */}
                {billingCycleDay !== savedCycleDay && (
                  <button
                    type="button"
                    onClick={() => {
                      userEditedCycleDayRef.current = false;
                      setBillingCycleDay(savedCycleDay);
                      setCycleFieldError(null);
                    }}
                    disabled={savingCycle}
                    // Visible text "Cancel" carries the action; the
                    // aria-label augments it with the field context so
                    // screen reader users hear "Cancel billing cycle day"
                    // instead of a bare "Cancel" that could be confused
                    // with the rename/danger-zone Cancel buttons on this
                    // same page. WCAG 2.5.3: visible text "Cancel" is
                    // included as the first word of the accessible name.
                    aria-label="Cancel billing cycle day edit"
                    className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                  >
                    Cancel
                  </button>
                )}
              </div>
            </form>

            {/*
              TBD-234's roster (`/settings/organization/periods`) is the only
              entry point to that page, so without this link the feature is
              deep-link-only. Deliberately quiet: `btnLink`, bottom of the
              card, no icon and no blurb, so it never competes with Close
              period or Save. It is a read-only diagnostic; do not promote it
              into a button.

              The `admin` guard is belt-and-braces, NOT the enforcing gate.
              This whole page already early-returns a spinner for non-admins
              (`if (loading || !user || !admin)` above), and the roster route
              gates itself the same way because
              GET /api/v1/settings/billing-periods/roster 403s non-admins. It
              is kept because
              the day this page grows a read-only member view, an ungated
              link would start bouncing members into a 403. Note for anyone
              writing a "hidden for non-admins" test: assert it against the
              rendered page, and know the early return is what makes it pass.
            */}
            {admin && (
              <p>
                <Link href="/settings/organization/periods" className={btnLink}>
                  View period roster →
                </Link>
              </p>
            )}
          </div>
        </div>

        {/* Manual Balance Adjustment (Track E) */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Manual balance adjustment</h2>
          </div>
          <div className="p-6 space-y-4">
            <p className="text-sm text-text-secondary">
              By default, account balances are derived from your transactions.
              Enabling this lets admins set an account&apos;s balance directly.
              Every adjustment generates a transaction so the audit trail
              stays intact, but using this feature means your balances no
              longer fully reflect imported activity. Off by default.
            </p>
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm text-text-primary">
                {allowAdjustEnabled ? "Enabled" : "Disabled"}
              </span>
              <button
                type="button"
                onClick={() => handleToggleAllowAdjust(!allowAdjustEnabled)}
                disabled={adjustSaving}
                aria-pressed={allowAdjustEnabled}
                aria-label={
                  allowAdjustEnabled
                    ? "Disable manual balance adjustment"
                    : "Enable manual balance adjustment"
                }
                className={`${
                  allowAdjustEnabled ? btnSecondary : btnPrimary
                } w-full sm:w-auto min-h-[44px] sm:min-h-0`}
              >
                {adjustSaving
                  ? "Saving..."
                  : allowAdjustEnabled
                  ? "Disable"
                  : "Enable"}
              </button>
            </div>
          </div>
        </div>

        {/* Automatic tasks (scheduler) — this whole page is already
            admin-gated by the early return above, matching how the
            Manual Balance Adjustment card just above is scoped. */}
        <SchedulerSettingsCard />

        {/* Advanced Configuration */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Advanced Configuration</h2>
          </div>
          <div className="p-6 space-y-4">
            <form onSubmit={handleAdd} className="flex flex-col gap-3 sm:flex-row sm:items-end sm:gap-3">
              <div className="flex-1">
                <label htmlFor="org-setting-key" className={label}>Key</label>
                <input id="org-setting-key" value={key} onChange={(e) => setKey(e.target.value)} className={`${input} w-full`} placeholder="key" />
              </div>
              <div className="flex-1">
                <label htmlFor="org-setting-value" className={label}>Value</label>
                <input id="org-setting-value" value={value} onChange={(e) => setValue(e.target.value)} className={`${input} w-full`} placeholder="value" />
              </div>
              <button type="submit" className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}>Add</button>
            </form>

            {settings.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs uppercase text-text-muted">
                      <th className="pb-2">Key</th>
                      <th className="pb-2">Value</th>
                      <th className="pb-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {settings.map((s) => (
                      <tr key={s.key} className="border-b border-border">
                        <td className="py-2 text-text-primary">{s.key}</td>
                        <td className="py-2">
                          {editingKey === s.key ? (
                            <input
                              value={editingValue}
                              onChange={(e) => setEditingValue(e.target.value)}
                              onKeyDown={(e) => e.key === "Enter" && handleUpdate(s.key)}
                              className={`${input} w-48`}
                              autoFocus
                            />
                          ) : (
                            <span className="text-text-secondary">{s.value}</span>
                          )}
                        </td>
                        <td className="py-2 text-right space-x-2">
                          {editingKey === s.key ? (
                            <>
                              <button onClick={() => handleUpdate(s.key)} className="text-xs text-accent hover:underline">Save</button>
                              <button onClick={() => setEditingKey(null)} className="text-xs text-text-muted hover:underline">Cancel</button>
                            </>
                          ) : (
                            <>
                              <button onClick={() => { setEditingKey(s.key); setEditingValue(s.value); }} className="text-xs text-accent hover:underline">Edit</button>
                              <button onClick={() => handleDelete(s.key)} className="text-xs text-danger hover:underline">Delete</button>
                            </>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>

      {user && (
        <div className="mt-6 space-y-6">
          <MembersSection
            currentUserId={user.id}
            currentRole={user.role as "owner" | "admin" | "member"}
          />
          <SmartRulesSection />
        </div>
      )}

      {isOrgOwner && user && (
        <div className="mt-6">
          <DemoDataCard user={user} />
        </div>
      )}

      {isOrgOwner && (
        <div className="mt-6">
          <section className={`${card} border-danger/40`}>
            <div className={cardHeader}>
              <h2 className={`${cardTitle} text-danger`}>Danger zone</h2>
            </div>
            <div className="px-6 py-5 space-y-3">
              <p className="text-sm text-text-secondary">
                Resetting wipes <strong>transactions, accounts, account types, categories, smart rules, budgets, forecast plans, recurring transactions, and billing periods</strong>. Your organization, members, subscription, settings, feature overrides, and pending invitations are preserved. The action cannot be undone.
              </p>
              <p className="text-sm text-text-secondary">
                Type <code className="rounded bg-surface-raised px-1.5 py-0.5 font-mono text-text-primary">RESET {orgName}</code> to confirm:
              </p>
              <input
                type="text"
                aria-label="Confirm reset phrase"
                value={resetPhrase}
                onChange={(e) => setResetPhrase(e.target.value)}
                placeholder={expectedResetPhrase}
                className={`${input} max-w-md`}
              />
              {resetError && <p className="text-sm text-danger">{resetError}</p>}
              <div>
                <button
                  type="button"
                  onClick={async () => {
                    if (!resetPhraseMatches) return;
                    setResetError("");
                    setResetting(true);
                    try {
                      await apiFetch("/api/v1/orgs/data/reset", {
                        method: "POST",
                        body: JSON.stringify({ confirm_phrase: resetPhrase.trim() }),
                      });
                      // Clear every SWR cache key without revalidating —
                      // the reset wiped accounts/categories/etc. on the
                      // server, so any cached value in this client session
                      // (e.g. /import's account+category lists) would
                      // briefly show deleted rows. Skipping revalidation
                      // here is safe because we navigate away immediately
                      // and the destination's hooks will refetch fresh.
                      await mutate(() => true, undefined, { revalidate: false });
                      router.push("/dashboard?reset=1");
                    } catch (err) {
                      setResetError(extractErrorMessage(err, "Reset failed"));
                      setResetting(false);
                    }
                  }}
                  disabled={!resetPhraseMatches || resetting}
                  className={btnDangerSolid}
                >
                  {resetting ? (
                    <span className="inline-flex items-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                      Resetting organization data...
                    </span>
                  ) : (
                    "Reset organization data permanently"
                  )}
                </button>
              </div>
            </div>
          </section>
        </div>
      )}

      <ConfirmModal
        open={!!confirmAction}
        title={confirmAction?.title ?? ""}
        message={confirmAction?.message ?? ""}
        variant={confirmAction?.variant ?? "warning"}
        onConfirm={() => {
          confirmAction?.action();
          setConfirmAction(null);
        }}
        onCancel={() => setConfirmAction(null)}
      />
      <ConfirmModal
        open={pendingToggleTo !== null}
        title={
          pendingToggleTo
            ? "Enable manual balance adjustment?"
            : "Disable manual balance adjustment?"
        }
        message={
          pendingToggleTo
            ? "Admins will be able to set account balances directly. Each adjustment generates a transaction for the difference, but balances may diverge from imported activity."
            : "Admins will no longer be able to set account balances directly. Existing adjustment transactions stay in place."
        }
        confirmLabel={pendingToggleTo ? "Enable" : "Disable"}
        variant="warning"
        onConfirm={() => {
          const next = pendingToggleTo;
          setPendingToggleTo(null);
          if (next !== null) applyAllowAdjustToggle(next);
        }}
        onCancel={() => setPendingToggleTo(null)}
      />
    </SettingsLayout>
  );
}
