/**
 * Pure classifier for React act() warnings. TBD-393.
 *
 * SIDE-EFFECT FREE, DELIBERATELY. This module is split from `counter.ts` so
 * that the canary self-test can unit-test the matcher WITHOUT importing the
 * module that installs the console patch.
 *
 * If they were one module, `self-test.test.tsx` importing `classify` would
 * install the patch as a side effect, and the canary would keep passing even
 * with the `vitest.setup.ts` import deleted -- i.e. it would stay green in
 * exactly the scenario it exists to detect. Keep them separate.
 */

const ACT_ANCHOR = "inside a test was not wrapped in act(";
const ACT_PREFIX = /^An update to (.+?) inside a test was not wrapped in act\(/;

export const UNPARSED = "<unparsed>";
export const UNNAMED = "<unnamed>";

/**
 * Classify one `console.error` argument list.
 *
 * Returns the component name to charge, or `null` when this is not an act
 * warning.
 *
 * THE MESSAGE ARRIVES UNFORMATTED. Measured against React 19.2.5:
 *
 *   args[0] === "An update to %s inside a test was not wrapped in act(...).\n\n"
 *   args[1] === "DashboardDataProvider"
 *
 * The component name is a SEPARATE ARGUMENT and `%s` is still literal in
 * args[0]. A matcher that regexes args[0] alone sees `%s` forever and counts
 * zero -- silently, on every run, while looking correct. That is the single
 * likeliest way this gate ships worthless.
 */
export function classify(args: unknown[]): string | null {
  const first = args[0];

  if (typeof first === "string" && first.includes(ACT_ANCHOR)) {
    // React 19's real shape: name in args[1], `%s` still in args[0].
    if (first.includes("%s")) {
      const name = args[1];
      return name === null || name === undefined ? UNNAMED : String(name);
    }
    // A pre-formatted variant (react-test-renderer, or a future React).
    const m = ACT_PREFIX.exec(first);
    if (m) return m[1];
    return UNPARSED;
  }

  // Last resort: the anchor survived somewhere in the joined arguments but
  // neither shape above matched. MISFILE rather than drop -- `<unparsed>` has
  // no baseline entry, so matcher drift becomes a build failure instead of
  // looking like a fix.
  const joined = args.map((a) => (typeof a === "string" ? a : String(a))).join(" ");
  return joined.includes(ACT_ANCHOR) ? UNPARSED : null;
}
