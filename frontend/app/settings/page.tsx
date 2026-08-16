"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import SettingsLayout from "@/components/SettingsLayout";
import PasswordInput from "@/components/ui/PasswordInput";
import RestartTourCard from "@/components/settings/RestartTourCard";
import { useAuth } from "@/components/auth/AuthProvider";
import SsoStepupErrorBanner from "@/components/auth/SsoStepupErrorBanner";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { input, label, btnPrimary, btnSecondary, card, cardTitle, error as errorCls, success as successCls } from "@/lib/styles";
import type { User } from "@/lib/types";

/**
 * Friendly copy keyed by the `?sso_stepup_error=<code>` value that
 * /api/v1/auth/sso-stepup/callback redirects back with on failure.
 * Mirrors the LoginPageBody mapping but adjusted for the
 * email-change-confirmation context (the user is already signed in).
 */
const SSO_STEPUP_ERROR_COPY: Record<string, string> = {
  state: "Your Google verification attempt expired. Try again to change your email.",
  token: "Google verification didn't complete. Try again.",
  userinfo: "Google verification didn't complete. Try again.",
  unverified:
    "Your Google account isn't verified, so we can't use it to confirm this change.",
  email_mismatch:
    "The Google account you signed in with doesn't match this profile. Use the same Google account.",
  cancelled:
    "You cancelled the Google verification. Try again whenever you're ready.",
  provider_error:
    "Google returned an error during verification. Try again.",
};
const SSO_STEPUP_ERROR_FALLBACK = "Google verification didn't complete. Try again.";

export default function SettingsProfilePage() {
  const { user, refreshMe } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  // `?sso_stepup_error=<code>` arrives via the 307 from
  // /api/v1/auth/sso-stepup/callback when the Google round-trip
  // fails. Surface a friendly banner per code and clear the query
  // string after dismiss/retry so a page refresh doesn't reshow it.
  const stepupErrorCode = searchParams?.get("sso_stepup_error");
  const [stepupErrorVisible, setStepupErrorVisible] = useState<boolean>(false);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- mirror the SSO step-up error banner visibility to the ?sso_stepup_error URL query
    setStepupErrorVisible(Boolean(stepupErrorCode));
  }, [stepupErrorCode]);
  function clearStepupErrorFromUrl() {
    setStepupErrorVisible(false);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.delete("sso_stepup_error");
    router.replace(url.pathname + (url.search || "") + url.hash);
  }

  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  // Only consulted when the email is being changed. Backend rejects
  // email changes that lack a correct current password — this mirrors
  // the /me/password endpoint's re-auth requirement and closes the
  // email-change account-takeover chain (S-P1-2).
  const [currentPassword, setCurrentPassword] = useState("");
  // SSO users (`password_set=false`) cannot type a current password.
  // They re-authenticate via Google: clicking "Verify with Google"
  // POSTs /api/v1/auth/sso-stepup/initiate, the browser navigates to
  // Google, and the callback redirects back to this page with the
  // token in the URL fragment. We read it on mount, clear the hash
  // so it never lingers in browser history, and pass it to the
  // backend in place of `current_password`.
  const [stepupToken, setStepupToken] = useState("");
  const [stepupBusy, setStepupBusy] = useState(false);

  useEffect(() => {
    if (user) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- seed the editable profile fields from AuthContext user once /me lands
      setFirstName(user.first_name ?? "");
      setLastName(user.last_name ?? "");
      setUsername(user.username);
      setEmail(user.email);
      setPhone(user.phone ?? "");
    }
  }, [user]);

  // Pull `#stepup_token=…` off the URL on mount and immediately
  // strip it from history. Fragments are never sent to the server,
  // so this is the safe channel the backend redirects to.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const hash = window.location.hash;
    if (hash.startsWith("#stepup_token=")) {
      const token = hash.slice("#stepup_token=".length);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- capture the step-up token from the URL fragment on mount before stripping it from history
      if (token) setStepupToken(token);
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }, []);

  const [profileMsg, setProfileMsg] = useState("");
  const [cancellingPending, setCancellingPending] = useState(false);
  const [profileErr, setProfileErr] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);

  const emailChanging = email !== (user?.email ?? "");
  const passwordSet = user?.password_set ?? true;

  async function handleVerifyWithGoogle() {
    setProfileErr(""); setStepupBusy(true);
    try {
      const data = await apiFetch<{ redirect_url: string }>(
        "/api/v1/auth/sso-stepup/initiate",
        { method: "POST" },
      );
      // Full navigation, not router.push — Google must own the next page.
      window.location.href = data.redirect_url;
    } catch (err) {
      setProfileErr(extractErrorMessage(err));
      setStepupBusy(false);
    }
  }

  // TBD-361. No password and no step-up, deliberately: requesting a change
  // moves the account's recovery channel and so demands proof of presence,
  // while cancelling one only restores the status quo and can move nothing.
  // Demanding a password to undo a mistake is the exact shape that made the
  // original defect unrecoverable.
  async function cancelPendingEmail() {
    setProfileErr("");
    setProfileMsg("");
    setCancellingPending(true);
    try {
      await apiFetch("/api/v1/users/me/pending-email", { method: "DELETE" });
      await refreshMe();
      setProfileMsg("Pending email change cancelled.");
    } catch (err) {
      setProfileErr(extractErrorMessage(err));
    } finally {
      setCancellingPending(false);
    }
  }

  async function handleProfileSubmit(e: FormEvent) {
    e.preventDefault();
    setProfileMsg(""); setProfileErr(""); setSavingProfile(true);
    try {
      // Only send fields that actually changed. Keeps legacy users with
      // grandfathered 1-2 char usernames able to save email/phone/name
      // without sending their username through the stricter validator.
      const payload: Record<string, string | null> = {};
      const normalize = (v: string) => v || null;
      if (normalize(firstName) !== (user?.first_name ?? null)) payload.first_name = normalize(firstName);
      if (normalize(lastName) !== (user?.last_name ?? null)) payload.last_name = normalize(lastName);
      if (username !== user?.username) payload.username = username;
      if (email !== user?.email) payload.email = email;
      if (normalize(phone) !== (user?.phone ?? null)) payload.phone = normalize(phone);

      if (Object.keys(payload).length === 0) {
        setProfileMsg("No changes to save");
        return;
      }

      if ("email" in payload) {
        if (user?.password_set === false) {
          if (!stepupToken) {
            setProfileErr(
              "Verify with Google before changing your email. Click 'Verify with Google' below.",
            );
            return;
          }
          payload.stepup_token = stepupToken;
        } else {
          if (!currentPassword) {
            setProfileErr(
              "Enter your current password to change your email.",
            );
            return;
          }
          payload.current_password = currentPassword;
        }
      }

      await apiFetch<User>("/api/v1/users/me", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      await refreshMe();
      setCurrentPassword("");
      setStepupToken("");
      // TBD-361. The old copy said "You'll need to sign in again" and
      // implied the address had changed. All three clauses became false
      // when the change became two-phase: the profile was NOT updated,
      // refreshMe() snaps the field back to the live address, and the
      // session survives. Left alone the user sees their edit revert with
      // no explanation, which reads as a failed save.
      setProfileMsg(
        "email" in payload
          ? `Check ${payload.email} for a confirmation link. Your current address stays your sign-in email until you confirm, and you are still signed in.`
          : "Profile updated",
      );
    } catch (err) {
      const message = extractErrorMessage(err);
      // Clear the step-up token whenever the error mentions step-up
      // verification. Backend may have rejected the token as expired
      // or no-longer-on-the-row, in which case the UI claiming
      // "Google verified" lies to the user. Re-prompting is cheap.
      // (Finding 3 from PR #138.)
      if (stepupToken && /step-up|verify with google/i.test(message)) {
        setStepupToken("");
        setProfileErr(`${message} Please verify with Google again to retry.`);
      } else {
        setProfileErr(message);
      }
    }
    finally { setSavingProfile(false); }
  }

  const displayName = [user?.first_name, user?.last_name].filter(Boolean).join(" ") || user?.username || "";
  const initials = [user?.first_name?.[0], user?.last_name?.[0]].filter(Boolean).join("").toUpperCase() || user?.username?.charAt(0).toUpperCase() || "?";

  return (
    <SettingsLayout activeTab="/settings">
      <div className="space-y-6">
        {stepupErrorVisible && stepupErrorCode && (
          <SsoStepupErrorBanner
            errorCode={stepupErrorCode}
            copyByCode={SSO_STEPUP_ERROR_COPY}
            fallbackCopy={SSO_STEPUP_ERROR_FALLBACK}
            busy={stepupBusy}
            onRetry={() => {
              clearStepupErrorFromUrl();
              handleVerifyWithGoogle();
            }}
            onDismiss={clearStepupErrorFromUrl}
          />
        )}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
          {/* Left column: identity card + Edit-Profile form */}
          <div className="space-y-6">
            <div className={`${card} p-6`}>
              <div className="flex items-center gap-4">
                <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent-dim text-lg font-semibold text-accent">
                  {initials}
                </div>
                <div>
                  <p className="font-medium text-text-primary">{displayName}</p>
                  <p className="mt-0.5 text-xs text-text-muted">
                    {user?.role} · {user?.org_name}
                    {user?.is_superadmin && <span className="ml-1 text-accent">· superadmin</span>}
                  </p>
                  {user?.email_verified && (
                    // TBD-361. The badge attests to the LIVE address, which
                    // stays verified while a change is in flight -- that is
                    // the point of the two-phase design. But it never names
                    // its subject, so with a pending claim on screen a reader
                    // pairs it with the wrong address and the card reads as
                    // contradicting the pending notice below. The suffix
                    // makes the subject unambiguous without duplicating the
                    // address already shown in the Email field.
                    <p className="mt-0.5 text-[10px] text-success">
                      Email verified
                      {user.pending_email ? (
                        <span className="text-text-muted"> · change pending</span>
                      ) : null}
                    </p>
                  )}
                </div>
              </div>
            </div>

            <div className={`${card} p-6`}>
              <h2 className={`mb-5 ${cardTitle}`}>Edit Profile</h2>
              <form onSubmit={handleProfileSubmit} className="space-y-4">
                {profileMsg && <div className={successCls}>{profileMsg}</div>}
                {profileErr && <div className={errorCls}>{profileErr}</div>}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="profile-firstname" className={label}>First Name</label>
                    <input id="profile-firstname" type="text" value={firstName} onChange={(e) => setFirstName(e.target.value)} className={input} placeholder="John" />
                  </div>
                  <div>
                    <label htmlFor="profile-lastname" className={label}>Last Name</label>
                    <input id="profile-lastname" type="text" value={lastName} onChange={(e) => setLastName(e.target.value)} className={input} placeholder="Doe" />
                  </div>
                </div>
                <div>
                  <label htmlFor="profile-username" className={label}>Username</label>
                  <input id="profile-username" type="text" required value={username} onChange={(e) => setUsername(e.target.value)} className={`${input} max-w-md`} />
                </div>
                <div>
                  <label htmlFor="profile-email" className={label}>Email</label>
                  <input id="profile-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={`${input} max-w-md`} />
                  {/* TBD-361. The only place the browser can express "abandon
                      that claim". The form never transmits an unchanged
                      address, so without this a mistyped address stays
                      clickable for its full 24 hours and whoever owns it can
                      promote themselves onto the account. */}
                  {user?.pending_email && (
                    <div
                      data-testid="pending-email-row"
                      className="mt-2 max-w-md rounded-md border border-border bg-surface-raised px-3 py-2"
                    >
                      <p className="text-xs text-text-secondary">
                        Waiting for confirmation at{" "}
                        <span className="font-medium text-text-primary">{user.pending_email}</span>.
                        Your current address stays your sign-in email until then.
                      </p>
                      <button
                        type="button"
                        onClick={cancelPendingEmail}
                        disabled={cancellingPending}
                        className="mt-1 text-xs font-medium text-accent underline underline-offset-2 disabled:opacity-50"
                      >
                        {cancellingPending ? "Cancelling…" : "Cancel this change"}
                      </button>
                    </div>
                  )}
                </div>
                {emailChanging && passwordSet && (
                  <div>
                    <label htmlFor="profile-current-password" className={label}>
                      Current password <span className="text-xs text-text-muted">(required to change email)</span>
                    </label>
                    <PasswordInput
                      id="profile-current-password"
                      autoComplete="current-password"
                      required
                      value={currentPassword}
                      onChange={(e) => setCurrentPassword(e.target.value)}
                      className={`${input} max-w-md`}
                    />
                    <p className="mt-1 text-xs text-text-muted">
                      Prefer not to type your password? You can{" "}
                      <Link href="/settings/security" className="text-accent hover:underline">
                        change it
                      </Link>{" "}
                      any time from the Security settings.
                    </p>
                  </div>
                )}
                {emailChanging && !passwordSet && (
                  <div className="rounded-lg border border-border p-4 space-y-3">
                    <p className="text-sm text-text-primary font-medium">
                      Verify with Google to change your email
                    </p>
                    <p className="text-xs text-text-muted">
                      Your account was created with Google and has no password yet. Verify with Google now to confirm this change, or{" "}
                      <Link href="/settings/security" className="text-accent hover:underline">
                        set a password first
                      </Link>{" "}
                      in Security settings.
                    </p>
                    {stepupToken ? (
                      <p className="text-xs text-success">
                        Google verified. Click Save Changes to update your email.
                      </p>
                    ) : (
                      <button
                        type="button"
                        onClick={handleVerifyWithGoogle}
                        disabled={stepupBusy}
                        className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                      >
                        {stepupBusy ? "Redirecting..." : "Verify with Google"}
                      </button>
                    )}
                  </div>
                )}
                <div>
                  <label htmlFor="profile-phone" className={label}>Phone</label>
                  <input id="profile-phone" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} className={`${input} max-w-md`} placeholder="+1 234 567 8900" />
                </div>
                <button type="submit" disabled={savingProfile} className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}>
                  {savingProfile ? "Saving..." : "Save Changes"}
                </button>
              </form>
            </div>
          </div>

          {/* Right column: Dashboard-Tour card */}
          <div>
            <RestartTourCard />
          </div>
        </div>
      </div>
    </SettingsLayout>
  );
}
