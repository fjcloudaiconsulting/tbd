"use client";

import { FormEvent, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { User } from "@/lib/types";

export default function ProfilePage() {
  const { user, login } = useAuth();

  // Profile form
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

  // Password form
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
      // Re-login with new password to refresh tokens
      await login(user!.username, newPassword);
      setPwdMsg("Password changed successfully");
    } catch (err) {
      setPwdErr(err instanceof Error ? err.message : "Failed");
    } finally {
      setSavingPwd(false);
    }
  }

  return (
    <AppShell>
      <h1 className="mb-6 text-xl font-semibold">Profile</h1>

      <div className="max-w-lg space-y-6">
        {/* Account info */}
        <div className="rounded-lg border border-gray-200 bg-white p-5">
          <div className="mb-4 flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-blue-100 text-sm font-bold text-blue-700">
              {user?.username?.charAt(0).toUpperCase()}
            </div>
            <div>
              <p className="font-medium">{user?.username}</p>
              <p className="text-xs text-gray-400">
                {user?.role} · {user?.org_name}
                {user?.is_superadmin && (
                  <span className="ml-1 text-blue-600">· superadmin</span>
                )}
              </p>
            </div>
          </div>
        </div>

        {/* Edit profile */}
        <div className="rounded-lg border border-gray-200 bg-white p-5">
          <h2 className="mb-4 text-sm font-medium text-gray-700">
            Edit Profile
          </h2>
          <form onSubmit={handleProfileSubmit} className="space-y-3">
            {profileMsg && (
              <div className="rounded bg-green-50 p-2 text-sm text-green-700">
                {profileMsg}
              </div>
            )}
            {profileErr && (
              <div className="rounded bg-red-50 p-2 text-sm text-red-700">
                {profileErr}
              </div>
            )}
            <div>
              <label className="mb-1 block text-sm">Username</label>
              <input
                type="text"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm">Email</label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
              />
            </div>
            <button
              type="submit"
              disabled={savingProfile}
              className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {savingProfile ? "Saving..." : "Save Changes"}
            </button>
          </form>
        </div>

        {/* Change password */}
        <div className="rounded-lg border border-gray-200 bg-white p-5">
          <h2 className="mb-4 text-sm font-medium text-gray-700">
            Change Password
          </h2>
          <form onSubmit={handlePasswordSubmit} className="space-y-3">
            {pwdMsg && (
              <div className="rounded bg-green-50 p-2 text-sm text-green-700">
                {pwdMsg}
              </div>
            )}
            {pwdErr && (
              <div className="rounded bg-red-50 p-2 text-sm text-red-700">
                {pwdErr}
              </div>
            )}
            <div>
              <label className="mb-1 block text-sm">Current Password</label>
              <input
                type="password"
                required
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
                autoComplete="current-password"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm">New Password</label>
              <input
                type="password"
                required
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
                autoComplete="new-password"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm">Confirm New Password</label>
              <input
                type="password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
                autoComplete="new-password"
              />
            </div>
            <button
              type="submit"
              disabled={savingPwd}
              className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {savingPwd ? "Changing..." : "Change Password"}
            </button>
          </form>
        </div>
      </div>
    </AppShell>
  );
}
