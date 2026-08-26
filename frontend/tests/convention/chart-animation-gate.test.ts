import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-428 — structural companion to
 * ``tests/components/charts/reduced-motion.test.tsx``.
 *
 * The behavioural fence proves recharts' ``"auto"`` default honours
 * ``prefers-reduced-motion``, and that ``isAnimationActive={true}`` overrides
 * it. What that fence CANNOT see is a new call site: it renders a handful of
 * synthetic charts, not the app's fourteen recharts surfaces. So a
 * contributor writing ``isAnimationActive={true}`` on a new widget would ship
 * motion to users who opted out with every test green.
 *
 * ## This is a PARSE, not a grep — deliberately, and at the second attempt
 *
 * The first version of this gate was a regex over comment-stripped source.
 * Review killed it with a measured counter-example: the line comment
 * ``// Catalog of /admin/* sub-pages ...`` in ``app/admin/page.tsx:55``
 * contains ``/*``, which opened a block comment that ran to the next ``*​/``
 * 342 lines later — **blanking 325 of that file's 486 lines from the scan**.
 * Any violation in that range would have been invisible with the gate green.
 * The same shape is idiomatic here (``/system/*``, ``/settings/*``).
 *
 * Lexing JSX with regexes also mis-handled ``isAnimationActive = {false}``
 * (flagged, wrongly), a value on the line below its prop name (missed), and
 * ``//`` inside a string literal (truncated the line).
 *
 * So this walks the real TypeScript AST. Comments and string literals are not
 * code and never reach the visitor; whitespace and line breaks are irrelevant;
 * the prop's value is read as a node, not a substring. This is the repo's own
 * standing rule — a grep can be satisfied by a comment; parse the structure.
 *
 * ## Verdicts
 *
 * * **Prop absent** — resolves to ``"auto"``, which gates on the preference.
 *   Correct, and the commonest shape. Not visited at all.
 * * ``isAnimationActive={false}`` — never animates, so a reduced-motion user
 *   is never harmed. Allowed. It does remove motion for everyone, which is a
 *   design choice rather than an accessibility one (TBD-382 made it for the
 *   report widgets); this gate does not police that.
 * * ``isAnimationActive={true}`` or the bare shorthand — **rejected.** Both
 *   mean literal ``true``, which overrides ``"auto"`` and animates regardless
 *   of the user's preference.
 * * A **computed** value (``{shouldAnimate}``, ``{a && b}``, a spread) —
 *   rejected too, but with a different message. No call site does this today,
 *   so the set is empty and nothing false-REDs. It is refused rather than
 *   waved through because a computed gate is exactly the change that needs a
 *   matching behavioural fence, and silence here is how that gets skipped.
 */

const FRONTEND_ROOT = path.resolve(__dirname, "..", "..");

/** Walk the WHOLE frontend, minus what cannot contain app code.
 *
 *  ⚠ An exclusion list, not an inclusion list, and that is load-bearing.
 *  Review found the earlier ``ROOTS = ["app", "components"]`` form made the
 *  companion coverage assertion a tautology: the chart files were *derived
 *  from* the scanned set, so "every chart file is scanned" could not fail.
 *  Moving `app/` to Next's supported `src/app/` layout — a routine refactor —
 *  would have dropped `app/dashboard/page.tsx`, `app/budgets/`, and
 *  `app/forecast-plans/` (3 of the 7 animating surfaces) out of the scan with
 *  every test still green. */
const SKIP_DIRS = new Set([
  "node_modules",
  ".next",
  "out",
  "out-apex",
  "public",
  "coverage",
  "tests",
]);
const EXTENSIONS = [".tsx", ".ts"];

function walk(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry.startsWith(".") || SKIP_DIRS.has(entry)) continue;
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (EXTENSIONS.some((e) => full.endsWith(e))) out.push(full);
  }
  return out;
}

type Offence = { file: string; line: number; kind: "literal-true" | "computed" };

function inspect(file: string): Offence[] {
  const source = readFileSync(file, "utf8");
  // Cheap pre-filter on RAW source. Safe in this direction: a file that does
  // not contain the substring at all cannot contain the JSX attribute, and a
  // file that mentions it only in prose is parsed and then found clean.
  if (!source.includes("isAnimationActive")) return [];

  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const found: Offence[] = [];

  const visit = (node: ts.Node): void => {
    if (
      ts.isJsxAttribute(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === "isAnimationActive"
    ) {
      const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
      const init = node.initializer;

      if (init === undefined) {
        // Bare shorthand `<Bar isAnimationActive />` — JSX shorthand for true.
        found.push({ file, line, kind: "literal-true" });
      } else if (ts.isJsxExpression(init) && init.expression) {
        const expr = init.expression;
        if (expr.kind === ts.SyntaxKind.TrueKeyword) {
          found.push({ file, line, kind: "literal-true" });
        } else if (expr.kind !== ts.SyntaxKind.FalseKeyword) {
          found.push({ file, line, kind: "computed" });
        }
      } else {
        // A string initializer, e.g. isAnimationActive="auto".
        if (!(ts.isStringLiteral(init) && init.text === "auto")) {
          found.push({ file, line, kind: "computed" });
        }
      }
    }
    ts.forEachChild(node, visit);
  };

  visit(sf);
  return found;
}

const scanned = walk(FRONTEND_ROOT);
const offences = scanned.flatMap(inspect);
const rel = (f: string) => path.relative(FRONTEND_ROOT, f);

describe("TBD-428: no chart forces animation on past prefers-reduced-motion", () => {
  it("no call site sets isAnimationActive to a literal true", () => {
    const bad = offences
      .filter((o) => o.kind === "literal-true")
      .map((o) => `${rel(o.file)}:${o.line}`);

    expect(
      bad,
      'isAnimationActive={true} (and the bare shorthand) override recharts\' ' +
        '"auto" default and animate for users who asked for reduced motion. ' +
        'Drop the prop -- "auto" already gates on the preference -- or set it ' +
        "to false if the chart should never animate for anyone.\n" +
        bad.join("\n"),
    ).toEqual([]);
  });

  it("no call site computes isAnimationActive without a behavioural fence", () => {
    const bad = offences
      .filter((o) => o.kind === "computed")
      .map((o) => `${rel(o.file)}:${o.line}`);

    expect(
      bad,
      "isAnimationActive is being computed rather than written literally. " +
        "This gate cannot tell whether the result honours prefers-reduced-" +
        "motion, so the guarantee has to come from a behavioural test in " +
        "tests/components/charts/reduced-motion.test.tsx. Add one, then " +
        "allowlist the site here with a reason.\n" +
        bad.join("\n"),
    ).toEqual([]);
  });

  it("the parse actually reaches the chart surface", () => {
    // Guards the guard. If `walk` silently returns nothing, or the parse
    // stops visiting JSX, both assertions above pass vacuously while the gate
    // reports green.
    //
    // ⚠ The coverage claim is only non-circular because `walk` starts at the
    // frontend root and EXCLUDES: the chart files are found by content across
    // the whole tree rather than inside a hand-listed subset of it.
    const chartFiles = scanned.filter((f) =>
      /from\s+["']recharts["']/.test(readFileSync(f, "utf8")),
    );

    expect(chartFiles.length).toBeGreaterThan(0);

    // The parse must actually see attributes, not merely open files.
    const allAttrs = scanned.flatMap((f) => {
      const src = readFileSync(f, "utf8");
      if (!src.includes("isAnimationActive")) return [];
      const sf = ts.createSourceFile(f, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
      const hits: string[] = [];
      const visit = (n: ts.Node): void => {
        if (ts.isJsxAttribute(n) && ts.isIdentifier(n.name) && n.name.text === "isAnimationActive") {
          hits.push(rel(f));
        }
        ts.forEachChild(n, visit);
      };
      visit(sf);
      return hits;
    });
    expect(allAttrs.length).toBeGreaterThan(0);

    // The surfaces TBD-428 was filed against, named so a refactor that moves
    // them out of the walk fails here rather than silently shrinking coverage.
    const chartRel = chartFiles.map(rel);
    for (const required of [
      "components/dashboard/widgets/BudgetBarsWidget.tsx",
      "components/dashboard/widgets/ForecastBarsWidget.tsx",
      "app/dashboard/page.tsx",
      "app/budgets/BudgetOverviewChart.tsx",
      "app/forecast-plans/ForecastPlanChart.tsx",
    ]) {
      expect(chartRel).toContain(required);
    }
  });

  it("a comment or string cannot hide a violation from the parse", () => {
    // Direct regression test for the defect that killed the regex version:
    // `/admin/*` in a line comment opened a fake block comment and blanked
    // 325 lines of app/admin/page.tsx. A parse cannot do this, and this test
    // is what proves the property rather than asserting the parser exists.
    const hostile = [
      "// Catalog of /admin/* sub-pages. isAnimationActive={true} in prose.",
      "const doc = \"pass isAnimationActive={true} to force it\";",
      "/* isAnimationActive={true} inside a block comment */",
      "export const A = () => <Bar isAnimationActive={false} />;",
      "export const B = () => <Bar />;",
    ].join("\n");

    const sf = ts.createSourceFile("hostile.tsx", hostile, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const hits: string[] = [];
    const visit = (n: ts.Node): void => {
      if (ts.isJsxAttribute(n) && ts.isIdentifier(n.name) && n.name.text === "isAnimationActive") {
        hits.push(n.getText(sf));
      }
      ts.forEachChild(n, visit);
    };
    visit(sf);

    // Exactly one real attribute, and it is the allowed `false` one.
    expect(hits).toEqual(["isAnimationActive={false}"]);
  });
});
