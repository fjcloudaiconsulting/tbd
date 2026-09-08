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
