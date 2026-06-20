import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ConsentBanner from "@/components/landing/ConsentBanner";
import {
  CONSENT_OPEN_EVENT,
  CONSENT_STORAGE_KEY,
  readConsent,
} from "@/lib/consent";

beforeEach(() => {
  localStorage.clear();
  // Fresh gtag spy per test.
  (window as unknown as { gtag: (...a: unknown[]) => void }).gtag = vi.fn();
});

describe("<ConsentBanner />", () => {
  it("shows when no consent is stored", () => {
    render(<ConsentBanner />);
    expect(screen.getByRole("region", { name: /cookie consent/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^accept$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^reject$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^customize$/i })).toBeInTheDocument();
  });

  it("stays hidden when a valid choice is already stored", () => {
    localStorage.setItem(
      CONSENT_STORAGE_KEY,
      JSON.stringify({ analytics: true, marketing: false, ts: Date.now() }),
    );
    render(<ConsentBanner />);
    expect(screen.queryByRole("region", { name: /cookie consent/i })).not.toBeInTheDocument();
  });

  it("Accept persists both granted and updates Consent Mode", () => {
    render(<ConsentBanner />);
    fireEvent.click(screen.getByRole("button", { name: /^accept$/i }));

    const stored = readConsent(Date.now());
    expect(stored).toMatchObject({ analytics: true, marketing: true });

    const gtag = (window as unknown as { gtag: ReturnType<typeof vi.fn> }).gtag;
    expect(gtag).toHaveBeenCalledWith("consent", "update", {
      analytics_storage: "granted",
      ad_storage: "granted",
      ad_user_data: "granted",
      ad_personalization: "granted",
    });
    // Banner closes after a choice.
    expect(screen.queryByRole("region", { name: /cookie consent/i })).not.toBeInTheDocument();
  });

  it("Reject persists both denied and updates Consent Mode", () => {
    render(<ConsentBanner />);
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));

    expect(readConsent(Date.now())).toMatchObject({ analytics: false, marketing: false });
    const gtag = (window as unknown as { gtag: ReturnType<typeof vi.fn> }).gtag;
    expect(gtag).toHaveBeenCalledWith("consent", "update", {
      analytics_storage: "denied",
      ad_storage: "denied",
      ad_user_data: "denied",
      ad_personalization: "denied",
    });
  });

  it("Customize starts opt-out and lets a user grant analytics only", () => {
    render(<ConsentBanner />);
    fireEvent.click(screen.getByRole("button", { name: /^customize$/i }));

    // Necessary is locked on.
    const necessary = screen.getByLabelText(/necessary cookies/i) as HTMLInputElement;
    expect(necessary).toBeChecked();
    expect(necessary).toBeDisabled();

    // GDPR: non-essential categories start UNticked for a first-time visitor.
    const analyticsBox = screen.getByLabelText(/analytics cookies/i) as HTMLInputElement;
    const marketingBox = screen.getByLabelText(/marketing cookies/i) as HTMLInputElement;
    expect(analyticsBox).not.toBeChecked();
    expect(marketingBox).not.toBeChecked();

    // Actively grant analytics only, leave marketing off.
    fireEvent.click(analyticsBox);
    fireEvent.click(screen.getByRole("button", { name: /save preferences/i }));

    expect(readConsent(Date.now())).toMatchObject({ analytics: true, marketing: false });
    const gtag = (window as unknown as { gtag: ReturnType<typeof vi.fn> }).gtag;
    expect(gtag).toHaveBeenCalledWith("consent", "update", {
      analytics_storage: "granted",
      ad_storage: "denied",
      ad_user_data: "denied",
      ad_personalization: "denied",
    });
  });

  it("re-opens on the open-consent event even after a stored choice", () => {
    localStorage.setItem(
      CONSENT_STORAGE_KEY,
      JSON.stringify({ analytics: true, marketing: true, ts: Date.now() }),
    );
    render(<ConsentBanner />);
    expect(screen.queryByRole("region", { name: /cookie consent/i })).not.toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event(CONSENT_OPEN_EVENT));
    });
    expect(screen.getByRole("region", { name: /cookie consent/i })).toBeInTheDocument();
    // Re-opens straight into the preferences panel, prefilled from the stored
    // choice (both granted) rather than the opt-out first-visit defaults.
    expect(screen.getByRole("button", { name: /save preferences/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/analytics cookies/i)).toBeChecked();
    expect(screen.getByLabelText(/marketing cookies/i)).toBeChecked();
  });
});
