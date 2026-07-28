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

  // A 409 carries the conflicting budget category or period start from the
  // server. Before TBD-232 it fell through `default:` and rendered exactly
  // the sentence a 500 renders, throwing that detail away.
  it("surfaces the server detail on a 409 instead of the generic fallback", () => {
    // Fixture mirrors the REAL wire shape. `billing_service.py` raises
    // ConflictError("A budget already exists at the new period start for: "
    // + ", ".join(names), code="budget_period_conflict"); main.py's handler
    // emits { detail: "<that sentence>", code: "budget_period_conflict" };
    // apiFetch lifts the flat `code` onto ApiResponseError. The server
    // message carries NO date, so this must not assert one.
    const msg = mapBillingCycleError(
      new ApiResponseError(
        409,
        "A budget already exists at the new period start for: Groceries",
        "budget_period_conflict",
      ),
    );
    expect(msg).toMatch(/Groceries/);
    expect(msg).toMatch(/already exists at the new period start/i);
    expect(msg).not.toMatch(/could not save the billing cycle/i);
  });

  it("falls back to a conflict sentence when the 409 body is empty", () => {
    const msg = mapBillingCycleError(new ApiResponseError(409, "   "));
    expect(msg).toMatch(/collides/i);
    expect(msg).not.toMatch(/—|–/);
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

  // TBD-241 D7. The server message is PINNED as "Close date cannot be in the
  // future" so the predicate and the sentence are not written against each
  // other by guesswork.
  it("maps the future-date 400 to its own hint, not the generic fallback", () => {
    const msg = mapBillingPeriodCloseError(
      new ApiResponseError(400, "Close date cannot be in the future"),
    );
    expect(msg).toMatch(/future/i);
    expect(msg).not.toMatch(/already closed/i);
    expect(msg).not.toMatch(/We could not close the period/i);
    expect(msg).not.toMatch(/—|–/);
  });

  it("keeps the unrecognised 400 on the fallback", () => {
    expect(mapBillingPeriodCloseError(new ApiResponseError(400, "who knows"))).toMatch(
      /We could not close the period/i,
    );
  });

  // Defence in depth, not a reachable path: by D5's own argument the identity
  // pre-flight matches nothing, the IntegrityError backstop is unreachable
  // under the normative operation order, and the identity UPDATE changes only
  // `period_end` so it cannot violate uq_budget_org_cat_period. The mapper had
  // no `case 409` at all, so a stray one reached the user as a bare fallback.
  it("surfaces a 409 body instead of flattening it into the fallback", () => {
    const msg = mapBillingPeriodCloseError(
      new ApiResponseError(409, "A budget already exists at the new period start for: Rent"),
    );
    expect(msg).toMatch(/Rent/);
    expect(msg).not.toMatch(/We could not close the period/i);
  });

  it("falls back to a conflict sentence when the 409 body is empty", () => {
    const msg = mapBillingPeriodCloseError(new ApiResponseError(409, "   "));
    expect(msg).toMatch(/Something else changed this period/i);
    expect(msg).not.toMatch(/We could not close the period/i);
    expect(msg).not.toMatch(/—|–/);
  });
});
