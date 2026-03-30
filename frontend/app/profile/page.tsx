"use client";

import { FormEvent, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { User } from "@/lib/types";

export default function ProfilePage() {
  const { user, login, refreshMe } = useAuth();

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");

  useEffect(() => {
    if (user) {
      setUsername(user.username);
      setEmail(user.email);
    }
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
    setProfileMsg("");
    setProfileErr("");
    setSavingProfile(true);
    try {
      await apiFetch<User>("/api/v1/users/me", {
        method: "PUT",
        body: JSON.stringify({ username, email }),
      });
      await refreshMe();
      setProfileMsg("Profile updated");
    } catch (err) {
      setProfileErr(err instanceof Error ? err.message : "Failed");
    } finally {
      setSavingProfile(false);
    }
  }

  async function handlePasswordSubmit(e: FormEvent) {
    e.preventDefault();
    setPwdMsg("");
    setPwdErr("");

    if (newPassword !== confirmPassword) {
      setPwdErr("New passwords do not match");
      return;
    }

    setSavingPwd(true);
    try {
      await apiFetch("/api/v1/users/me/password", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      setPwdMsg("Password changed. Signing in with new credentials...");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      await login(user!.username, newPassword);
      setPwdMsg("Password changed successfully");
    } catch (err) {
      setPwdErr(err instanceof Error ? err.message : "Failed");
    } finally {
      setSavingPwd(false);
    }
  }

  const inputClass =
    "w-full rounded-md border border-border bg-surface-raised px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none";

  return (
    <AppShell>
      <h1 className="mb-8 font-display text-2xl text-text-primary">Profile</h1>

      <div className="max-w-lg space-y-6">
        {/* Account info */}
        <div className="rounded-lg border border-border bg-surface p-6">
          <div className="flex items-center gap-4">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent-dim font-display text-lg text-accent">
              {user?.username?.charAt(0).toUpperCase()}
            </div>
            <div>
              <p className="font-medium text-text-primary">{user?.username}</p>
              <p className="mt-0.5 text-xs text-text-muted">
                {user?.role} · {user?.org_name}
                {user?.is_superadmin && (
                  <span className="ml-1 text-accent">· superadmin</span>
                )}
              </p>
            </div>
          </div>
        </div>

        {/* Edit profile */}
        <div className="rounded-lg border border-border bg-surface p-6">
          <h2 className="mb-5 text-xs font-medium uppercase tracking-wider text-text-muted">
            Edit Profile
          </h2>
          <form onSubmit={handleProfileSubmit} className="space-y-4">
            {profileMsg && (
              <div className="rounded-md bg-success-dim px-4 py-3 text-sm text-success">
                {profileMsg}
              </div>
            )}
            {profileErr && (
              <div className="rounded-md bg-danger-dim px-4 py-3 text-sm text-danger">
                {profileErr}
              </div>
            )}
            <div>
              <label className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Username
              </label>
              <input
                type="text"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className={inputClass}
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Email
              </label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={inputClass}
              />
            </div>
            <button
              type="submit"
              disabled={savingProfile}
              className="rounded-md bg-accent px-5 py-2.5 text-sm font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50"
            >
              {savingProfile ? "Saving..." : "Save Changes"}
            </button>
          </form>
        </div>

        {/* Change password */}
        <div className="rounded-lg border border-border bg-surface p-6">
          <h2 className="mb-5 text-xs font-medium uppercase tracking-wider text-text-muted">
            Change Password
          </h2>
          <form onSubmit={handlePasswordSubmit} className="space-y-4">
            {pwdMsg && (
              <div className="rounded-md bg-success-dim px-4 py-3 text-sm text-success">
                {pwdMsg}
              </div>
            )}
            {pwdErr && (
              <div className="rounded-md bg-danger-dim px-4 py-3 text-sm text-danger">
                {pwdErr}
              </div>
            )}
            <div>
              <label className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Current Password
              </label>
              <input
                type="password"
                required
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                className={inputClass}
                autoComplete="current-password"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                New Password
              </label>
              <input
                type="password"
                required
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                className={inputClass}
                autoComplete="new-password"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Confirm New Password
              </label>
              <input
                type="password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className={inputClass}
                autoComplete="new-password"
              />
            </div>
            <button
              type="submit"
              disabled={savingPwd}
              className="rounded-md bg-accent px-5 py-2.5 text-sm font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50"
            >
              {savingPwd ? "Changing..." : "Change Password"}
            </button>
          </form>
        </div>
      </div>
    </AppShell>
  );
}
