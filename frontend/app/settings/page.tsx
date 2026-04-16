"use client";

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

  async function handleProfileSubmit(e: FormEvent) {
    e.preventDefault();
    setProfileMsg(""); setProfileErr(""); setSavingProfile(true);
    try {
      await apiFetch<User>("/api/v1/users/me", {
        method: "PUT",
        body: JSON.stringify({
          first_name: firstName || null,
          last_name: lastName || null,
          username,
          email,
          phone: phone || null,
        }),
      });
      await refreshMe();
      setProfileMsg("Profile updated");
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
            <div className="flex gap-3">
              <div className="flex-1">
                <label htmlFor="profile-firstname" className={label}>First Name</label>
                <input id="profile-firstname" type="text" value={firstName} onChange={(e) => setFirstName(e.target.value)} className={input} placeholder="John" />
              </div>
              <div className="flex-1">
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
            <div>
              <label htmlFor="profile-phone" className={label}>Phone</label>
              <input id="profile-phone" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} className={input} placeholder="+1 234 567 8900" />
            </div>
            <button type="submit" disabled={savingProfile} className={btnPrimary}>
              {savingProfile ? "Saving..." : "Save Changes"}
            </button>
          </form>
        </div>
      </div>
    </SettingsLayout>
  );
}
