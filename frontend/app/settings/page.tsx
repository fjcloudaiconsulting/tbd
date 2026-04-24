"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import SettingsLayout from "@/components/SettingsLayout";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { input, label, btnPrimary, card, cardTitle, error as errorCls, success as successCls } from "@/lib/styles";
import type { User } from "@/lib/types";

export default function SettingsProfilePage() {
  const { user, refreshMe } = useAuth();

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

  useEffect(() => {
    if (user) {
      setFirstName(user.first_name ?? "");
      setLastName(user.last_name ?? "");
      setUsername(user.username);
      setEmail(user.email);
      setPhone(user.phone ?? "");
    }
  }, [user]);

  const [profileMsg, setProfileMsg] = useState("");
  const [profileErr, setProfileErr] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);

  const emailChanging = email !== (user?.email ?? "");

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
        if (!currentPassword) {
          setProfileErr(
            "Enter your current password to change your email. If you signed in with Google and never set one, reset your password first.",
          );
          return;
        }
        payload.current_password = currentPassword;
      }

      await apiFetch<User>("/api/v1/users/me", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      await refreshMe();
      setCurrentPassword("");
      // Nudge the user toward the next step — changing email logs every
      // session out and leaves email_verified=false until they click
      // the new verification link.
      setProfileMsg(
        "email" in payload
          ? "Profile updated. Check your new inbox for a verification link — you'll need to sign in again."
          : "Profile updated",
      );
    } catch (err) { setProfileErr(extractErrorMessage(err)); }
    finally { setSavingProfile(false); }
  }

  const displayName = [user?.first_name, user?.last_name].filter(Boolean).join(" ") || user?.username || "";
  const initials = [user?.first_name?.[0], user?.last_name?.[0]].filter(Boolean).join("").toUpperCase() || user?.username?.charAt(0).toUpperCase() || "?";

  return (
    <SettingsLayout activeTab="/settings">
      <div className="max-w-lg space-y-6">
        <div className={`${card} p-6`}>
          <div className="flex items-center gap-4">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent-dim font-display text-lg text-accent">
              {initials}
            </div>
            <div>
              <p className="font-medium text-text-primary">{displayName}</p>
              <p className="mt-0.5 text-xs text-text-muted">
                {user?.role} · {user?.org_name}
                {user?.is_superadmin && <span className="ml-1 text-accent">· superadmin</span>}
              </p>
              {user?.email_verified && (
                <p className="mt-0.5 text-[10px] text-success">Email verified</p>
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
              <input id="profile-username" type="text" required value={username} onChange={(e) => setUsername(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="profile-email" className={label}>Email</label>
              <input id="profile-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={input} />
            </div>
            {emailChanging && (
              <div>
                <label htmlFor="profile-current-password" className={label}>
                  Current password <span className="text-xs text-text-muted">(required to change email)</span>
                </label>
                <input
                  id="profile-current-password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  className={input}
                />
                <p className="mt-1 text-xs text-text-muted">
                  Signed in with Google and never set a password?{" "}
                  <Link href="/forgot-password" className="text-accent hover:underline">
                    Reset it first
                  </Link>
                  , then come back to change your email.
                </p>
              </div>
            )}
            <div>
              <label htmlFor="profile-phone" className={label}>Phone</label>
              <input id="profile-phone" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} className={input} placeholder="+1 234 567 8900" />
            </div>
            <button type="submit" disabled={savingProfile} className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
              {savingProfile ? "Saving..." : "Save Changes"}
            </button>
          </form>
        </div>
      </div>
    </SettingsLayout>
  );
}
