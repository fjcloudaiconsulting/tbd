"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  badgeNeutral,
  badgeSuccess,
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
} from "@/lib/styles";

const CONSENT_URL = "/api/v1/settings/ai-providers/consent";

interface ConsentSnapshot {
  has_consent: boolean;
  consent_version: string | null;
  current_consent_version: string;
}

/** The org's AI master switch (TBD-627): On = a current consent row. */
export default function AiMasterSwitchCard() {
  const [snap, setSnap] = useState<ConsentSnapshot | null>(null);
  const [agreed, setAgreed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const s = await apiFetch<ConsentSnapshot>(CONSENT_URL);
      if (s) setSnap(s);
    } catch (e) {
      setErr(extractErrorMessage(e, "Could not load the AI setting"));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial data fetch: load() writes the consent snapshot into state
    void load();
  }, [load]);

  if (!snap) return err ? <div className={`${errorCls} mb-4`}>{err}</div> : null;

  // A stale version reads as Off: the backend refuses until the org re-consents.
  const on = snap.has_consent && snap.consent_version === snap.current_consent_version;

  async function post(revoked: boolean) {
    if (!snap) return;
    setBusy(true);
    setErr(null);
    try {
      const body = revoked
        ? { consent_version: snap.current_consent_version, revoked: true }
        : { consent_version: snap.current_consent_version };
      await apiFetch(CONSENT_URL, { method: "POST", body: JSON.stringify(body) });
      setAgreed(false);
      await load();
    } catch (e) {
      setErr(extractErrorMessage(e, "Could not change the AI setting"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`${card} mb-6`} aria-labelledby="ai-master-switch-title">
      <div className={`${cardHeader} flex items-center justify-between`}>
        <h3 id="ai-master-switch-title" className={cardTitle}>
          AI for this organization
        </h3>
        <span className={on ? badgeSuccess : badgeNeutral}>{on ? "On" : "Off"}</span>
      </div>
      <div className="space-y-4 px-6 py-4 text-sm text-text-secondary">
        <p>
          AI is off until you turn it on. When it is on, the data an AI feature needs is sent to
          an AI provider. When it is off, none of your organization&rsquo;s data is sent to any
          AI provider, and connected AI apps cannot read your data.
        </p>
        {err && <div className={errorCls}>{err}</div>}
        {on ? (
          <button type="button" className={btnSecondary} disabled={busy} onClick={() => post(true)}>
            Turn AI off
          </button>
        ) : (
          <>
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                className="mt-0.5 h-4 w-4 accent-accent"
                checked={agreed}
                onChange={(e) => setAgreed(e.target.checked)}
              />
              <span>
                I agree that, while AI is on, the data each AI feature needs is sent to an AI
                provider, as described above.
              </span>
            </label>
            <Link href="/privacy#ai-providers" className="block underline underline-offset-2">
              How AI uses your data
            </Link>
            <button
              type="button"
              className={btnPrimary}
              disabled={!agreed || busy}
              onClick={() => post(false)}
            >
              Turn AI on
            </button>
          </>
        )}
      </div>
    </section>
  );
}
