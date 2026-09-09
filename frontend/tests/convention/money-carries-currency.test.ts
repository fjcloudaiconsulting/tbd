/**
 * No component renders `formatAmount` output directly into the UI (TBD-503).
 *
 * `formatAmount` is the BARE number formatter — "1,234.56", no currency. It is
 * the right primitive and the wrong thing to render: a money slot must go
 * through `formatMoney` (explicit currency) or `useMoney()` (the org's).
 *
 * This is the DoD item "so the next new tile cannot quietly regress". The
 * regression it guards is not hypothetical: before this ticket, 134 call sites
 * across 32 files rendered bare numbers, which is why some surfaces showed a
 * currency and most did not. Nothing stopped the next one.
 *
 * ⚠ WHAT THIS CAN AND CANNOT SEE — read before trusting it.
 * This is a SOURCE-LEVEL convention check, and its blind spot is indirection:
 *
 *     const shown = formatAmount(x);   // assigned, not rendered inline
 *     return <span>{shown}</span>;     // invisible to this fence
 *
 * It catches the shape that actually keeps happening (`{formatAmount(x)}`
 * written straight into JSX) and nothing subtler. It is a ratchet, not a
 * proof. The behavioural fences — `tests/lib/money.test.ts`,
 * `tests/lib/hooks/use-org-currency.test.tsx`, the anchored per-surface
 * assertions, and `root-layout-currency-altitude.test.tsx` — are what actually
 * establish that the figures are right.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { globSync } from "glob";

const ROOT = join(__dirname, "..", "..");

/**
 * `{formatAmount(` in a JSX expression container, NOT `${formatAmount(`
 * inside a template literal. The `$` is the entire difference between
 * rendering a bare number and composing one into a larger string, and the two
 * legitimate composition sites below are exactly the latter.
 */
const JSX_RENDER = /(?<!\$)\{\s*formatAmount\s*\(/;

/**
 * Composition sites that build a fuller string around the bare magnitude.
 * Both are deliberate and both are commented at the source:
 *   - `signedMoney` composes `{sign}{symbol}{magnitude}` so that negatives
 *     read "-€100.00" rather than "€-100.00".
 *   - `spokenAmount` is screen-reader-only text, where the ISO code reads
 *     better than a symbol a reader would announce as "euro sign".
 * Neither renders `formatAmount` output on its own.
 */
const ALLOWED = new Set<string>([
  "components/dashboard/AccountMonthEndForecast.tsx",
  "components/dashboard/widgets/BalancesByTypeTile.tsx",
]);

describe("money slots carry a currency", () => {
  it("no .tsx renders formatAmount() directly into JSX", () => {
    const files = globSync("{app,components}/**/*.tsx", { cwd: ROOT });

    // ⚠ Guard the SWEEP, not just its result. An empty or mis-rooted glob
    // makes the assertion below vacuously true — a fence that inspects
    // nothing passes forever. This repo has shipped that exact shape.
    expect(files.length).toBeGreaterThan(200);

    const offenders = files.filter((rel) => {
      if (ALLOWED.has(rel)) return false;
      return JSX_RENDER.test(readFileSync(join(ROOT, rel), "utf8"));
    });

    expect(
      offenders,
      "these render a bare number into the UI; use formatMoney(value, currency) " +
        "or useMoney() so the figure carries its currency",
    ).toEqual([]);
  });

  it("the allowlist stays honest: every entry still composes formatAmount", () => {
    // An allowlist that outlives its reason silently widens the fence. If one
    // of these files stops using formatAmount, it should leave the list rather
    // than sit here licensing a future bare render.
    for (const rel of ALLOWED) {
      const src = readFileSync(join(ROOT, rel), "utf8");
      expect(src, `${rel} no longer uses formatAmount — drop it from ALLOWED`)
        .toMatch(/\$\{[^}]*formatAmount\s*\(/);
    }
  });
});
