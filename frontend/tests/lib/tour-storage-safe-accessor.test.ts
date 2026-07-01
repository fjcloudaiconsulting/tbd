/**
 * Safe tour-flag storage accessor (fix/appshell-tour-storage-independence).
 *
 * The tour "pending start" flag used to hit sessionStorage directly behind
 * a bare try/catch. When storage is unavailable or throws (Safari private
 * mode, disabled site data, quota errors) the write silently vanished, so
 * the flag path was dead AND a flag that could not be cleared could
 * re-trigger. These helpers add a module-scoped in-memory fallback so the
 * flag stays consistent for the rest of the SPA session and never crashes.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  safeTourStorageGet,
  safeTourStorageRemove,
  safeTourStorageSet,
} from "@/lib/help/tourStorage";

const KEY = "tbd-test-tour-flag";

afterEach(() => {
  vi.restoreAllMocks();
  // Clear both backends so module-scoped memory never leaks between tests.
  safeTourStorageRemove(KEY);
});

describe("safe tour-flag storage accessor", () => {
  it("round-trips through sessionStorage on the happy path", () => {
    safeTourStorageSet(KEY, "extended");
    // Happy path is unchanged: the value lands in real sessionStorage.
    expect(window.sessionStorage.getItem(KEY)).toBe("extended");
    expect(safeTourStorageGet(KEY)).toBe("extended");
    safeTourStorageRemove(KEY);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
    expect(safeTourStorageGet(KEY)).toBeNull();
  });

  it("returns the value from memory when setItem THROWS (private mode)", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("QuotaExceededError");
    });
    // Write must not throw...
    expect(() => safeTourStorageSet(KEY, "1")).not.toThrow();
    // ...and the value survives via the in-memory fallback.
    expect(safeTourStorageGet(KEY)).toBe("1");
  });

  it("falls back to memory when getItem THROWS", () => {
    // Seed via memory (setItem throws so only memory holds it).
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("SecurityError");
    });
    safeTourStorageSet(KEY, "extended");
    // Now getItem itself throws; get must not crash and must read memory.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("SecurityError");
    });
    expect(safeTourStorageGet(KEY)).toBe("extended");
  });

  it("remove clears the in-memory flag so it does not re-trigger", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("SecurityError");
    });
    safeTourStorageSet(KEY, "1");
    expect(safeTourStorageGet(KEY)).toBe("1");
    safeTourStorageRemove(KEY);
    // Dismissed stays dismissed for the session.
    expect(safeTourStorageGet(KEY)).toBeNull();
  });

  it("returns null (no crash) when nothing is set and storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("SecurityError");
    });
    expect(safeTourStorageGet(KEY)).toBeNull();
  });
});
