/**
 * Supported account currencies (TBD-325).
 *
 * ⚠ THIS LIST MUST MATCH `backend/app/services/currency_service.py`.
 * `backend/tests/test_currency_list_frontend_contract.py` asserts that it
 * does, in both directions, and FAILS rather than regenerating — the same
 * posture as `frontend/tests/fixtures/report-sources.json`. If that test is
 * red, one side gained or lost a currency: read the diff and decide, do not
 * auto-sync.
 *
 * Why the list is duplicated at all: the backend is the authority (it rejects
 * anything outside the set), but the picker has to render the options without
 * a round-trip, and adding an endpoint to serve 170 static strings is a worse
 * trade than one drift fence.
 *
 * An organization may hold accounts in exactly ONE currency. Every period
 * aggregate in the product sums without joining `accounts`, so a mixed-currency
 * org would see EUR and USD added together as a single unlabelled number. The
 * server enforces this; the picker just makes it visible.
 */

/** The dozen a user is most likely to want, surfaced above the long tail. */
export const COMMON_CURRENCIES: ReadonlyArray<readonly [string, string]> = [
  ["EUR", "Euro"],
  ["USD", "US Dollar"],
  ["GBP", "Pound Sterling"],
  ["BRL", "Brazilian Real"],
  ["CHF", "Swiss Franc"],
  ["CAD", "Canadian Dollar"],
  ["AUD", "Australian Dollar"],
  ["JPY", "Japanese Yen"],
  ["CNY", "Chinese Yuan"],
  ["INR", "Indian Rupee"],
  ["SEK", "Swedish Krona"],
  ["NOK", "Norwegian Krone"],
];

/** Every ISO 4217 code the backend accepts, sorted. */
export const ALL_CURRENCIES: readonly string[] = [
  "AED", "AFN", "ALL", "AMD", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM",
  "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL", "BSD",
  "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY", "COP",
  "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN",
  "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF",
  "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD",
  "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW",
  "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD",
  "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR",
  "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD",
  "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON",
  "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP",
  "SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS",
  "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD",
  "UYU", "UZS", "VED", "VES", "VND", "VUV", "WST", "XAF", "XCD", "XCG",
  "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWG",
];

/** Codes not already shown in the common group, for the second optgroup. */
export const OTHER_CURRENCIES: readonly string[] = ALL_CURRENCIES.filter(
  (c) => !COMMON_CURRENCIES.some(([code]) => code === c),
);

/**
 * The currency symbol for a code, or `null` when the currency has none.
 *
 * Derived from `Intl` at render time rather than a hand-kept table: a symbol
 * table is one more thing to drift, and the platform already knows. Pinned to
 * `en` so the label matches the rest of the English UI instead of shifting
 * with the viewer's locale.
 *
 * ⚠ Most supported codes have NO symbol — `Intl` echoes the code back
 * ("BWP" -> "BWP"), which would render "BWP BWP", so that case returns null and
 * the caller omits the slot. The exact count is deliberately not written here:
 * it depends on the platform's ICU data and on the supported set, and a number
 * in a comment is the kind of thing that goes stale silently. `currencies.test.ts`
 * asserts the RULES instead.
 *
 * Ambiguity is not a concern here and it was worth checking: `Intl` already
 * disambiguates the dollar family (USD "$", CAD "CA$", AUD "A$") and the yen
 * family (JPY "¥", CNY "CN¥"). Measured across all 170: zero symbols are
 * shared by two currencies.
 */
export function currencySymbol(code: string): string | null {
  try {
    const part = new Intl.NumberFormat("en", {
      style: "currency",
      currency: code,
    })
      .formatToParts(1)
      .find((p) => p.type === "currency");
    const value = part?.value ?? code;
    if (value === code) return null;
    // ⚠ Length guard. For a handful of currencies `Intl` returns a wordy
    // ABBREVIATION rather than a symbol — "F CFA" (XOF), "FCFA" (XAF),
    // "CFPF" (XPF). Rendered next to the code those read as noise
    // ("F CFA XOF") and undo the reason for showing a symbol at all. A real
    // symbol is short: €, $, £, ¥, ₹, ₪, ₩, ₱, ₫, and the disambiguated
    // dollar/yen forms R$, A$, CA$, HK$, MX$, NZ$, NT$, EC$, CN¥ all fit in
    // three characters.
    return value.length <= 3 ? value : null;
  } catch {
    // An unknown code throws RangeError. The picker only ever passes codes
    // from ALL_CURRENCIES, but a bad value should degrade to "no symbol"
    // rather than blanking the whole list.
    return null;
  }
}

/**
 * `"€ EUR · Euro"`, or `"CHF · Swiss Franc"` when there is no symbol.
 *
 * ⚠ Middle dot, NOT an em-dash. Em-dashes are banned in user-facing copy
 * (`feedback_no_em_dashes`) and fenced by
 * tests/voice/no-em-dash-in-customer-copy.test.ts, which caught exactly this
 * line. The policy leaves no en-dash fallback for prose either.
 */
export function currencyLabel(code: string, name?: string): string {
  const sym = currencySymbol(code);
  const head = sym ? `${sym} ${code}` : code;
  return name ? `${head} · ${name}` : head;
}

/**
 * The prefix a money figure leads with: `"€"`, or `"CHF "` when the currency
 * has no symbol, or `""` when no currency is known (TBD-503).
 *
 * ⚠ THE SINGLE SOURCE. This replaces three implementations that had drifted
 * apart — `lib/reports/series.ts` and `AccountMonthEndForecast.tsx` each
 * carried a hardcoded EUR/USD/GBP table (literal copies of one another), while
 * everything else rendered bare numbers. Do not add a fourth; import this.
 *
 * The code-plus-space fallback is inherited from those two deliberately: it is
 * what makes "at least the currency is present" true for all 156 supported
 * codes rather than only the 19 with symbols.
 *
 * ⚠ AN EMPTY RETURN IS LOAD-BEARING. `deriveOrgCurrency` yields `undefined`
 * for a MULTI-currency org, where no single symbol is correct — labelling
 * every figure with one would mislabel measures that aggregate
 * differently-denominated accounts. Returning a symbol for an absent currency
 * would defeat that gate at every render site.
 */
export function currencyPrefix(code: string | null | undefined): string {
  if (!code) return "";
  const symbol = currencySymbol(code);
  return symbol ? symbol : `${code} `;
}

/**
 * The one currency an org's accounts are denominated in, or `undefined`.
 *
 * ⚠ THE MULTI-CURRENCY GATE. A mixed-currency org returns `undefined` so every
 * figure degrades to a bare number rather than being mislabelled with one
 * currency's symbol. TBD-325 closed the door on new orgs reaching that state,
 * but the gate stays correct for any legacy row and must not be simplified
 * away on the grounds that it is now unreachable.
 *
 * Moved here from `lib/reports/series.ts` (as `reportCurrency`): the
 * derivation was never report-specific, and leaving it there is what let the
 * rest of the app grow its own answer.
 *
 * ⚠ When `Organization.primary_currency` lands (TBD-325 PR 2) this is the ONE
 * place that changes — every consumer reads the provider, not this function.
 */
export function deriveOrgCurrency(
  accounts: Array<{ currency?: string | null }> | undefined | null,
): string | undefined {
  // ⚠ `Array.isArray`, not `?? []`. The nullish guard only covers null and
  // undefined — any other non-array value (an error envelope, a paginated
  // object, a mocked `{}`) reaches the `for...of` and throws
  // "is not iterable", taking down every money figure on the page with it.
  // This is read on EVERY render of the whole authenticated tree, so it has to
  // tolerate whatever the accounts endpoint hands back.
  if (!Array.isArray(accounts)) return undefined;
  const distinct = new Set<string>();
  for (const a of accounts) {
    if (a && a.currency) distinct.add(a.currency);
  }
  return distinct.size === 1 ? [...distinct][0] : undefined;
}
