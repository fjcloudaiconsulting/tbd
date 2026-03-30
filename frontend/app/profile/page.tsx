"use client";

import { FormEvent, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { input, label, btnPrimary, card, cardTitle, error as errorCls, success as successCls, pageTitle } from "@/lib/styles";
import type { User } from "@/lib/types";

export default function ProfilePage() {
  const { user, login, refreshMe } = useAuth();

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");

  useEffect(() => {
    if (user) { setUsername(user.username); setEmail(user.email); }
  }, [user]);

  const [profileMsg, setProfileMsg] = useState("");
  const [profileErr, setProfileErr] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pwdMsg, setPwdMsg] = useState("");
  const [pwdErr, setPwdErr] = useState("");
  const [savingPwd, setSavingPwd] = useState(false);

  async function handleProfileSubmit(e: FormEvent) {
    e.preventDefault();
    setProfileMsg(""); setProfileErr(""); setSavingProfile(true);
    try {
      await apiFetch<User>("/api/v1/users/me", { method: "PUT", body: JSON.stringify({ username, email }) });
      await refreshMe();
      setProfileMsg("Profile updated");
    } catch (err) { setProfileErr(extractErrorMessage(err)); }
    finally { setSavingProfile(false); }
  }

  async function handlePasswordSubmit(e: FormEvent) {
    e.preventDefault();
    setPwdMsg(""); setPwdErr("");
    if (newPassword !== confirmPassword) { setPwdErr("New passwords do not match"); return; }
    setSavingPwd(true);
    try {
      await apiFetch("/api/v1/users/me/password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
      setPwdMsg("Password changed. Signing in with new credentials...");
      await login(user!.username, newPassword);
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword("");
      setPwdMsg("Password changed successfully");
    } catch (err) { setPwdErr(extractErrorMessage(err)); }
    finally { setSavingPwd(false); }
  }

  return (
    <AppShell>
      <h1 className={pageTitle}>Profile</h1>

      <div className="max-w-lg space-y-6">
        <div className={`${card} p-6`}>
          <div className="flex items-center gap-4">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent-dim font-display text-lg text-accent">
              {user?.username?.charAt(0).toUpperCase()}
            </div>
            <div>
              <p className="font-medium text-text-primary">{user?.username}</p>
              <p className="mt-0.5 text-xs text-text-muted">
                {user?.role} · {user?.org_name}
                {user?.is_superadmin && <span className="ml-1 text-accent">· superadmin</span>}
              </p>
            </div>
          </div>
        </div>

        <div className={`${card} p-6`}>
          <h2 className={`mb-5 ${cardTitle}`}>Edit Profile</h2>
          <form onSubmit={handleProfileSubmit} className="space-y-4">
            {profileMsg && <div className={successCls}>{profileMsg}</div>}
            {profileErr && <div className={errorCls}>{profileErr}</div>}
            <div>
              <label htmlFor="profile-username" className={label}>Username</label>
              <input id="profile-username" type="text" required value={username} onChange={(e) => setUsername(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="profile-email" className={label}>Email</label>
              <input id="profile-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={input} />
            </div>
            <button type="submit" disabled={savingProfile} className={btnPrimary}>
              {savingProfile ? "Saving..." : "Save Changes"}
            </button>
          </form>
        </div>

        <div className={`${card} p-6`}>
          <h2 className={`mb-5 ${cardTitle}`}>Change Password</h2>
          <form onSubmit={handlePasswordSubmit} className="space-y-4">
            {pwdMsg && <div className={successCls}>{pwdMsg}</div>}
            {pwdErr && <div className={errorCls}>{pwdErr}</div>}
            <div>
              <label htmlFor="pwd-current" className={label}>Current Password</label>
              <input id="pwd-current" type="password" required value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} className={input} autoComplete="current-password" />
            </div>
            <div>
              <label htmlFor="pwd-new" className={label}>New Password</label>
              <input id="pwd-new" type="password" required value={newPassword} onChange={(e) => setNewPassword(e.target.value)} className={input} autoComplete="new-password" />
            </div>
            <div>
              <label htmlFor="pwd-confirm" className={label}>Confirm New Password</label>
              <input id="pwd-confirm" type="password" required value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} className={input} autoComplete="new-password" />
            </div>
            <button type="submit" disabled={savingPwd} className={btnPrimary}>
              {savingPwd ? "Changing..." : "Change Password"}
            </button>
          </form>
        </div>
      </div>
    </AppShell>
  );
}
