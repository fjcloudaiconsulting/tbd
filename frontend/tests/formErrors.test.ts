import { describe, expect, it } from "vitest";
import { ApiResponseError } from "@/lib/api";
import {
  mapBillingCycleError,
  mapBillingPeriodCloseError,
  mapMfaDisableError,
  mapMfaRegenerateError,
  mapMfaSetupError,
  validateBillingCycleDay,
} from "@/lib/formErrors";

describe("validateBillingCycleDay", () => {
  it("accepts integer days 1 through 28", () => {
    for (const day of [1, 5, 15, 28]) {
      expect(validateBillingCycleDay(String(day))).toBeNull();
    }
  });

  it("rejects days outside 1-28", () => {
    expect(validateBillingCycleDay("0")).toMatch(/1 and 28/);
    expect(validateBillingCycleDay("29")).toMatch(/1 and 28/);
    expect(validateBillingCycleDay("31")).toMatch(/1 and 28/);
    expect(validateBillingCycleDay("-3")).toMatch(/digits only|1 and 28/);
  });

  it("rejects non-numeric and empty input with field-specific copy", () => {
    expect(validateBillingCycleDay("")).toMatch(/between 1 and 28/);
    expect(validateBillingCycleDay("abc")).toMatch(/digits only/);
    expect(validateBillingCycleDay("1.5")).toMatch(/digits only/);
  });

  it("contains no em-dashes", () => {
    const msgs = [
      validateBillingCycleDay(""),
      validateBillingCycleDay("0"),
      validateBillingCycleDay("abc"),
    ];
    for (const m of msgs) {
      expect(m).not.toMatch(/—|–/);
    }
  });
});

describe("mapMfaSetupError", () => {
  it("maps 401 to a friendly retry message without revealing details", () => {
    const err = new ApiResponseError(401, "Invalid TOTP code");
    const msg = mapMfaSetupError(err);
    expect(msg).toMatch(/did not match/i);
    expect(msg).not.toContain("Invalid TOTP code");
  });

  it("maps 400 with code language to a refresh hint", () => {
    const err = new ApiResponseError(400, "Invalid TOTP code");
    expect(mapMfaSetupError(err)).toMatch(/30 seconds/);
  });

  it("maps 400 'already enabled' to a refresh hint", () => {
    const err = new ApiResponseError(400, "MFA is already enabled");
    expect(mapMfaSetupError(err)).toMatch(/already on/i);
  });

  it("maps 429 to a wait message", () => {
    const err = new ApiResponseError(429, "Too many requests");
    expect(mapMfaSetupError(err)).toMatch(/wait a minute/i);
  });

  it("maps 503 to a temporary-unavailable message", () => {
    const err = new ApiResponseError(503, "anything");
    expect(mapMfaSetupError(err)).toMatch(/temporarily unavailable/i);
  });

  it("falls back to the supplied fallback for unrecognised statuses", () => {
    const err = new ApiResponseError(418, "I am a teapot");
    expect(mapMfaSetupError(err, { fallback: "Custom" })).toBe("Custom");
  });

  it("handles non-ApiResponseError gracefully", () => {
    expect(mapMfaSetupError(new Error("boom"))).toBe("boom");
    expect(mapMfaSetupError({ weird: true })).toMatch(/Something went wrong/);
  });

  it("never contains em-dashes", () => {
    const samples = [
      mapMfaSetupError(new ApiResponseError(400, "Invalid TOTP code")),
      mapMfaSetupError(new ApiResponseError(401, "x")),
      mapMfaSetupError(new ApiResponseError(429, "x")),
      mapMfaSetupError(new ApiResponseError(503, "x")),
    ];
    for (const m of samples) {
      expect(m).not.toMatch(/—|–/);
    }
  });
});

describe("mapMfaDisableError", () => {
  it("maps 401 and 403 to the same friendly password-mismatch message", () => {
    expect(mapMfaDisableError(new ApiResponseError(401, "x"))).toMatch(/password did not match/i);
    expect(mapMfaDisableError(new ApiResponseError(403, "Invalid password"))).toMatch(
      /password did not match/i,
    );
  });

  it("maps 400 'not enabled' to a refresh hint", () => {
    expect(mapMfaDisableError(new ApiResponseError(400, "MFA is not enabled"))).toMatch(
      /not on/i,
    );
  });

  it("never reveals whether the password reuse vs bad-password caused failure", () => {
    const msgs = [
      mapMfaDisableError(new ApiResponseError(401, "Invalid password")),
      mapMfaDisableError(new ApiResponseError(403, "Invalid password")),
    ];
    // Both paths must produce the exact same user-facing string.
    expect(msgs[0]).toBe(msgs[1]);
  });
});

describe("mapMfaRegenerateError", () => {
  it("maps 401 to a password-mismatch message", () => {
    expect(mapMfaRegenerateError(new ApiResponseError(401, "x"))).toMatch(
      /password did not match/i,
    );
  });

  it("maps 400 'not enabled' to a helpful next step", () => {
    expect(mapMfaRegenerateError(new ApiResponseError(400, "MFA is not enabled"))).toMatch(
      /not on/i,
    );
  });
});

describe("mapBillingCycleError", () => {
  it("maps 422 to a clear field-rule sentence", () => {
    expect(mapBillingCycleError(new ApiResponseError(422, "validation"))).toMatch(
      /between 1 and 28/,
    );
  });

  it("maps 403 to a permission message", () => {
    expect(mapBillingCycleError(new ApiResponseError(403, "x"))).toMatch(/do not have permission/);
  });

  it("maps 429 to a rate-limit message", () => {
    expect(mapBillingCycleError(new ApiResponseError(429, "x"))).toMatch(/wait a moment/i);
  });
});

describe("mapBillingPeriodCloseError", () => {
  it("maps already-closed 400 to a refresh hint", () => {
    expect(
      mapBillingPeriodCloseError(new ApiResponseError(400, "Period already closed")),
    ).toMatch(/already closed/i);
  });

  it("maps 403 to a permission message", () => {
    expect(mapBillingPeriodCloseError(new ApiResponseError(403, "x"))).toMatch(
      /do not have permission/,
    );
  });
});
