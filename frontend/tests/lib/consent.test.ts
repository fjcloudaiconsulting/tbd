import { beforeEach, describe, expect, it } from "vitest";

import {
  CONSENT_STORAGE_KEY,
  CONSENT_TTL_MS,
  DEFAULT_DENIED,
  readConsent,
  toConsentModeUpdate,
  writeConsent,
  type ConsentChoice,
} from "@/lib/consent";

beforeEach(() => {
  localStorage.clear();
});

describe("consent storage", () => {
  it("round-trips a written choice", () => {
    const now = 1_000_000;
    writeConsent({ analytics: true, marketing: false }, now);
    const got = readConsent(now);
    expect(got).toEqual({ analytics: true, marketing: false, ts: now });
  });

  it("returns null when nothing is stored", () => {
    expect(readConsent(1_000_000)).toBeNull();
  });

  it("returns null for a choice just past the 6-month TTL", () => {
    const written = 1_000_000;
    writeConsent({ analytics: true, marketing: true }, written);
    expect(readConsent(written + CONSENT_TTL_MS + 1)).toBeNull();
  });

  it("still returns a choice just inside the TTL", () => {
    const written = 1_000_000;
    writeConsent({ analytics: true, marketing: true }, written);
    expect(readConsent(written + CONSENT_TTL_MS - 1)).not.toBeNull();
  });

  it("returns null for malformed JSON", () => {
    localStorage.setItem(CONSENT_STORAGE_KEY, "{not json");
    expect(readConsent(1_000_000)).toBeNull();
  });

  it("returns null for a record missing required fields", () => {
    localStorage.setItem(CONSENT_STORAGE_KEY, JSON.stringify({ analytics: true }));
    expect(readConsent(1_000_000)).toBeNull();
  });
});

describe("toConsentModeUpdate", () => {
  it("maps analytics + marketing both granted", () => {
    const choice: ConsentChoice = { analytics: true, marketing: true, ts: 0 };
    expect(toConsentModeUpdate(choice)).toEqual({
      analytics_storage: "granted",
      ad_storage: "granted",
      ad_user_data: "granted",
      ad_personalization: "granted",
    });
  });

  it("maps analytics granted, marketing denied", () => {
    const choice: ConsentChoice = { analytics: true, marketing: false, ts: 0 };
    expect(toConsentModeUpdate(choice)).toEqual({
      analytics_storage: "granted",
      ad_storage: "denied",
      ad_user_data: "denied",
      ad_personalization: "denied",
    });
  });

  it("maps both denied", () => {
    const choice: ConsentChoice = { analytics: false, marketing: false, ts: 0 };
    expect(toConsentModeUpdate(choice)).toEqual({
      analytics_storage: "denied",
      ad_storage: "denied",
      ad_user_data: "denied",
      ad_personalization: "denied",
    });
  });
});

describe("DEFAULT_DENIED", () => {
  it("denies analytics and all ad storage, grants security + functionality", () => {
    expect(DEFAULT_DENIED.analytics_storage).toBe("denied");
    expect(DEFAULT_DENIED.ad_storage).toBe("denied");
    expect(DEFAULT_DENIED.ad_user_data).toBe("denied");
    expect(DEFAULT_DENIED.ad_personalization).toBe("denied");
    expect(DEFAULT_DENIED.personalization_storage).toBe("denied");
    expect(DEFAULT_DENIED.security_storage).toBe("granted");
    expect(DEFAULT_DENIED.functionality_storage).toBe("granted");
  });
});
