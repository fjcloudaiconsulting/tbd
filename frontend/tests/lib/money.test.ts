/**
 * Money formatting: one symbol source, one formatter (TBD-503).
 *
 * Before this, the frontend had THREE `currencySymbol` implementations —
 * `lib/reports/series.ts`, `components/dashboard/AccountMonthEndForecast.tsx`
 * (a literal copy of it, both covering only EUR/USD/GBP), and the `Intl`-based
 * one added by TBD-325. Everything else rendered bare numbers. That is why
 * some surfaces showed a currency and most did not.
 *
 * ⚠ PLACEMENT IS PINNED BY AN OPERATOR RULING (2026-09-08): the symbol always
 * LEADS, while grouping and decimal separators keep following the viewer's
 * locale. `Intl.NumberFormat(style: "currency")` is therefore NOT usable — its
 * whole job is to place the symbol per locale, which is the option that was
 * rejected. Do not "fix" the order by switching to it; that is how this ends
 * up with two formatting paths that disagree.
 */
import { describe, expect, it } from "vitest";

import { currencyPrefix, deriveOrgCurrency } from "@/lib/currencies";
import { formatAmount, formatMoney, toEditAmount } from "@/lib/format";

describe("currencyPrefix", () => {
  it("is the symbol, with no trailing space, when one exists", () => {
    expect(currencyPrefix("EUR")).toBe("€");
    expect(currencyPrefix("USD")).toBe("$");
    expect(currencyPrefix("BRL")).toBe("R$");
  });

  it("falls back to the code plus a space when there is no symbol", () => {
    // ⚠ The house convention, inherited from the two implementations this
    // replaces. It renders "CHF 1,234.56" — the code still reads as a prefix,
    // so "at least the currency is present" holds for all 156 codes, not just
    // the 19 with symbols.
    expect(currencyPrefix("CHF")).toBe("CHF ");
    expect(currencyPrefix("SEK")).toBe("SEK ");
  });

  it("is EMPTY for an absent currency, so formatting degrades to a bare number", () => {
    // ⚠ LOAD-BEARING, not a null-check nicety. `reportCurrency`/
    // `deriveOrgCurrency` return undefined for a MULTI-currency org, where no
    // single symbol is correct and labelling every figure with one would
    // mislabel measures that aggregate differently-denominated accounts.
    // Returning a symbol here would defeat that gate at every render site.
    expect(currencyPrefix(undefined)).toBe("");
    expect(currencyPrefix(null)).toBe("");
    expect(currencyPrefix("")).toBe("");
  });

  it("degrades to the code rather than throwing on an unknown code", () => {
    // `Intl` throws RangeError on a bad code. A throw here would blank every
    // money figure on the page.
    expect(currencyPrefix("ZZZ")).toBe("ZZZ ");
  });
});

describe("formatMoney", () => {
  it("leads with the prefix and keeps the number locale-formatted", () => {
    expect(formatMoney(1234.56, "EUR")).toBe(`€${formatAmount(1234.56)}`);
    expect(formatMoney(1234.56, "CHF")).toBe(`CHF ${formatAmount(1234.56)}`);
  });

  it("is exactly formatAmount when no currency is known", () => {
    // The degraded path must be byte-identical to today's output, or the
    // multi-currency gate changes what those orgs see.
    expect(formatMoney(1234.56, undefined)).toBe(formatAmount(1234.56));
  });

  it("accepts the string amounts the API actually returns", () => {
    // `Transaction.amount` arrives as a JSON string from a Pydantic Decimal
    // ("19.99") while the TypeScript type claims number.
    expect(formatMoney("19.99", "EUR")).toBe(`€${formatAmount("19.99")}`);
  });

  it("puts the sign OUTSIDE the symbol", () => {
    // ⚠ "-€50.00", not "€-50.00". I first wrote this test asserting the
    // latter; converting the reconcile rows showed why that is wrong. Those
    // rows build a signed string from a POSITIVE amount plus a `type`
    // discriminator ("+€45.06"), so with the sign inside the symbol the two
    // paths would render negatives differently on the same screen.
    //
    // The codebase already had both conventions and had already judged
    // between them: `AccountMonthEndForecast`'s `signedMoney` comment calls
    // `${symbol}${money(v)}` -> "€-100.00" out as the naive form to avoid.
    // This unifies on the one that comment endorses.
    expect(formatMoney(-50, "EUR")).toBe("-€50.00");
    expect(formatMoney(-50, "CHF")).toBe("-CHF 50.00");
  });

  it("still shows a bare negative when no currency is known", () => {
    // The degraded path has no prefix to sit outside of, so it must be
    // untouched by the sign handling.
    expect(formatMoney(-50, undefined)).toBe(formatAmount(-50));
  });
});

describe("⚠ what must NOT gain a currency", () => {
  it("toEditAmount stays a bare machine-readable string", () => {
    // It seeds `<input type="number">`. A symbol makes the input unparseable
    // and silently breaks every edit form. This is the defect this ticket is
    // most likely to introduce, so it is fenced rather than assumed.
    expect(toEditAmount(1234.56)).toBe("1234.56");
    expect(toEditAmount("19.99")).toBe("19.99");
    expect(toEditAmount(1234.56)).not.toContain("€");
    expect(toEditAmount(1234.56)).not.toContain(",");
  });

  it("formatAmount itself is unchanged", () => {
    // Kept as the pure number formatter. `formatMoney` composes it rather than
    // replacing it, so the degraded path and every non-money numeric display
    // keep working exactly as before.
    expect(formatAmount(1234.56)).not.toContain("€");
  });
});

describe("deriveOrgCurrency", () => {
  it("returns the single currency an org holds", () => {
    expect(deriveOrgCurrency([{ currency: "EUR" }, { currency: "EUR" }])).toBe("EUR");
  });

  it("returns undefined for a MIXED-currency org", () => {
    // ⚠ The gate. No single symbol is correct, so every figure degrades to a
    // bare number rather than being mislabelled. TBD-325 made this state
    // unreachable for new orgs; it must stay correct for any legacy row.
    expect(deriveOrgCurrency([{ currency: "EUR" }, { currency: "USD" }])).toBeUndefined();
  });

  it("returns undefined when there are no accounts yet", () => {
    expect(deriveOrgCurrency([])).toBeUndefined();
    expect(deriveOrgCurrency(undefined)).toBeUndefined();
    expect(deriveOrgCurrency(null)).toBeUndefined();
  });

  it("ignores accounts with no currency rather than counting them as distinct", () => {
    expect(
      deriveOrgCurrency([{ currency: "EUR" }, { currency: null }, {}]),
    ).toBe("EUR");
  });
});
