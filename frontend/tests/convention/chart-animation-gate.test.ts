import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-428 / TBD-437: structural companion to
 * ``tests/components/charts/reduced-motion.test.tsx``.
 *
 * The behavioural fence proves recharts' ``"auto"`` default honours
 * ``prefers-reduced-motion``, and that ``isAnimationActive={true}`` overrides
 * it. What that fence CANNOT see is a call site: it renders a handful of
 * synthetic charts, not the app's recharts surfaces. So a contributor writing
 * ``isAnimationActive={true}`` on a new widget would ship motion to users who
 * opted out with every test green.
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
 * ## Verdicts on ``isAnimationActive``
 *
 * * **Prop absent** — resolves to ``"auto"``, which gates on the preference.
 *   Correct, and the only shape the app uses since TBD-437. Not visited.
 * * ``isAnimationActive="auto"``: the same thing spelled out. Allowed.
 * * ``isAnimationActive={true}`` or the bare shorthand — **rejected.** Both
 *   mean literal ``true``, which overrides ``"auto"`` and animates regardless
 *   of the user's preference.
 * * ``isAnimationActive={false}``: **rejected since TBD-437.** It never harms
 *   a reduced-motion user, so TBD-428 allowed it and wrote that this gate
 *   "does not police that". That was the gap: TBD-382 turned animation off
 *   for every report widget "for consistency", removing motion for users who
 *   never opted out, and nothing caught it. ``"auto"`` already does the
 *   accessibility work; ``false`` only subtracts from everyone else.
 * * A **computed** value (``{shouldAnimate}``, ``{a && b}``): rejected with
 *   a different message. No call site does this today. It is refused rather
 *   than waved through because a computed gate is exactly the change that
 *   needs a matching behavioural fence.
 *
 * ## Two further fences (TBD-437)
 *
 * * **Every recharts ``Bar`` / ``Line`` / ``Area`` / ``Pie`` sets
 *   ``animationDuration={220}``**, matched by IMPORT from ``"recharts"`` so
 *   an alias or a namespace import cannot hide one. Deleting
 *   ``isAnimationActive={false}`` without a duration hands the mark recharts'
 *   1500ms default, and a new widget gets the same default silently. 220 is
 *   the house value. Marks that predate this fence and set no duration are
 *   listed in ``NO_DURATION_BASELINE`` with an exact count, not waved through.
 * * **Every JSX element imported from ``@nivo/*`` sets literal
 *   ``animate={false}``**, matched by IMPORT, not component name. nivo is not
 *   recharts: ``@nivo/core@0.99`` passes ``immediate: !animate`` to
 *   react-spring and never reads ``prefers-reduced-motion``, so enabling it
 *   would ship motion to users who opted out. See SankeyWidgetChart.tsx.
 *
 * Both import-resolved scans also refuse a dynamic ``import("recharts")`` /
 * ``import("@nivo/...")``, which would hand components to the tree through a
 * path they cannot follow.
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

const parse = (file: string, source: string) =>
  ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const lineOf = (sf: ts.SourceFile, n: ts.Node) =>
  sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;

type Offence = {
  file: string;
  line: number;
  kind: "literal-true" | "literal-false" | "computed";
};

function inspect(file: string): Offence[] {
  const source = sources.get(file)!;
  // Cheap pre-filter on RAW source. Safe in this direction: a file that does
  // not contain the substring at all cannot contain the JSX attribute, and a
  // file that mentions it only in prose is parsed and then found clean.
  if (!source.includes("isAnimationActive")) return [];
  return inspectSource(parse(file, source), file);
}

function inspectSource(sf: ts.SourceFile, file: string): Offence[] {
  const found: Offence[] = [];
  const kindOf = (expr: ts.Node): Offence["kind"] | null =>
    expr.kind === ts.SyntaxKind.TrueKeyword
      ? "literal-true"
      : expr.kind === ts.SyntaxKind.FalseKeyword
        ? "literal-false"
        : ts.isStringLiteral(expr) && expr.text === "auto"
          ? null
          : "computed";

  const visit = (node: ts.Node): void => {
    if (
      ts.isJsxAttribute(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === "isAnimationActive"
    ) {
      const init = node.initializer;
      const kind =
        init === undefined
          ? "literal-true" // bare shorthand `<Bar isAnimationActive />`
          : ts.isJsxExpression(init)
            ? init.expression
              ? kindOf(init.expression)
              : "computed"
            : kindOf(init);
      if (kind) found.push({ file, line: lineOf(sf, node), kind });
    }

    // `const chartProps = { isAnimationActive: false }`, later spread onto a
    // chart. The property name is specific enough that this cannot collide
    // with unrelated code.
    if (
      ts.isPropertyAssignment(node) &&
      (ts.isIdentifier(node.name) || ts.isStringLiteral(node.name)) &&
      node.name.text === "isAnimationActive"
    ) {
      const kind = kindOf(node.initializer);
      if (kind) found.push({ file, line: lineOf(sf, node), kind });
    }

    ts.forEachChild(node, visit);
  };

  visit(sf);
  return found;
}

// ---------------------------------------------------------------------------
// Import-resolved JSX elements (TBD-437). Matched by the binding a component
// is imported AS, never by the name it happens to have, so
// `import { ResponsiveSankey as Flow } from "@nivo/sankey"` or
// `import * as R from "recharts"; <R.Bar />` is still found, and a local
// component that merely shares a name is not.
// ---------------------------------------------------------------------------

type ImportedElement = {
  file: string;
  line: number;
  module: string;
  /** The exported name (`Bar`), regardless of any local alias. */
  imported: string;
  attrs: ts.JsxAttributes;
};

function importedElements(
  sf: ts.SourceFile,
  file: string,
  matchModule: (m: string) => boolean,
): { elements: ImportedElement[]; dynamicImports: number[] } {
  // local name -> binding; imported === "*" for a namespace import.
  const bindings = new Map<string, { module: string; imported: string }>();
  for (const stmt of sf.statements) {
    if (!ts.isImportDeclaration(stmt) || !ts.isStringLiteral(stmt.moduleSpecifier)) continue;
    const from = stmt.moduleSpecifier.text;
    const clause = stmt.importClause;
    if (!matchModule(from) || !clause) continue;
    if (clause.name) bindings.set(clause.name.text, { module: from, imported: "default" });
    const nb = clause.namedBindings;
    if (nb && ts.isNamespaceImport(nb)) bindings.set(nb.name.text, { module: from, imported: "*" });
    if (nb && ts.isNamedImports(nb)) {
      for (const el of nb.elements) {
        bindings.set(el.name.text, { module: from, imported: (el.propertyName ?? el.name).text });
      }
    }
  }

  const elements: ImportedElement[] = [];
  const dynamicImports: number[] = [];
  const visit = (node: ts.Node): void => {
    if (
      ts.isCallExpression(node) &&
      node.expression.kind === ts.SyntaxKind.ImportKeyword &&
      node.arguments[0] &&
      ts.isStringLiteral(node.arguments[0]) &&
      matchModule(node.arguments[0].text)
    ) {
      dynamicImports.push(lineOf(sf, node));
    }

    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tag = node.tagName;
      let hit: { module: string; imported: string } | undefined;
      if (ts.isIdentifier(tag)) {
        const b = bindings.get(tag.text);
        if (b && b.imported !== "*") hit = b;
      } else if (ts.isPropertyAccessExpression(tag) && ts.isIdentifier(tag.expression)) {
        const b = bindings.get(tag.expression.text);
        if (b?.imported === "*") hit = { module: b.module, imported: tag.name.text };
      }
      if (hit) elements.push({ file, line: lineOf(sf, node), ...hit, attrs: node.attributes });
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return { elements, dynamicImports };
}

/** The literal value of attribute `name`, or `undefined` when it is absent,
 *  not a `{literal}` expression, or followed by a spread that could override
 *  it (in which case the literal is no longer what renders). */
function literalAttr(attrs: ts.JsxAttributes, name: string) {
  let value: ts.Expression | undefined;
  let present = false;
  for (const p of attrs.properties) {
    if (ts.isJsxSpreadAttribute(p)) {
      value = undefined;
    } else if (ts.isIdentifier(p.name) && p.name.text === name) {
      present = true;
      value =
        p.initializer && ts.isJsxExpression(p.initializer) ? p.initializer.expression : undefined;
    }
  }
  return { present, value };
}

const scanned = walk(FRONTEND_ROOT);
// Read once at collection time. Every test below is then pure computation, so
// none of them can hit the per-test timeout on a loaded runner by re-reading
// the tree (the "reaches the chart surface" check once did, at 5s).
const sources = new Map(scanned.map((f) => [f, readFileSync(f, "utf8")]));
const rel = (f: string) => path.relative(FRONTEND_ROOT, f);
const offences = scanned.flatMap(inspect);

const RECHARTS_MARKS = new Set(["Bar", "Line", "Area", "Pie"]);
const HOUSE_DURATION = 220;
const isRecharts = (m: string) => m === "recharts";
const isNivo = (m: string) => m.startsWith("@nivo/");

/**
 * Marks that predate TBD-437, set no ``animationDuration``, and so run
 * recharts' 1500ms default. The architect ruling named the nine report and
 * scenario marks only, so these were recorded rather than silently re-timed.
 * EXACT counts per file: a new duration-less mark in one of these files
 * fails, and so does fixing one without lowering its count here.
 */
const NO_DURATION_BASELINE: Record<string, number> = {
  "app/dashboard/page.tsx": 1,
  "components/dashboard/widgets/CreditUtilizationBar.tsx": 2,
  "components/dashboard/widgets/SpendingDonutWidget.tsx": 1,
};

const importScans = scanned.map((file) => {
  const source = sources.get(file)!;
  const wantsRecharts = source.includes("recharts");
  const wantsNivo = source.includes("@nivo/");
  if (!wantsRecharts && !wantsNivo) return { file, recharts: null, nivo: null };
  const sf = parse(file, source);
  return {
    file,
    recharts: wantsRecharts ? importedElements(sf, file, isRecharts) : null,
    nivo: wantsNivo ? importedElements(sf, file, isNivo) : null,
  };
});
const rechartsMarks = importScans
  .flatMap((s) => s.recharts?.elements ?? [])
  .filter((e) => RECHARTS_MARKS.has(e.imported));
const nivoElements = importScans.flatMap((s) => s.nivo?.elements ?? []);

describe("TBD-428: no chart forces animation on past prefers-reduced-motion", () => {
  it("no call site sets isAnimationActive to a literal true", () => {
    const bad = offences
      .filter((o) => o.kind === "literal-true")
      .map((o) => `${rel(o.file)}:${o.line}`);

    expect(
      bad,
      'isAnimationActive={true} (and the bare shorthand) override recharts\' ' +
        '"auto" default and animate for users who asked for reduced motion. ' +
        'Drop the prop -- "auto" already gates on the preference.\n' +
        bad.join("\n"),
    ).toEqual([]);
  });

  it("TBD-437: no call site sets isAnimationActive to a literal false", () => {
    const bad = offences
      .filter((o) => o.kind === "literal-false")
      .map((o) => `${rel(o.file)}:${o.line}`);

    expect(
      bad,
      "isAnimationActive={false} removes chart motion for every user, " +
        "including the ones who never asked for reduced motion; recharts' " +
        '"auto" default already turns it off for those who did. Drop the ' +
        `prop and set animationDuration={${HOUSE_DURATION}} (TBD-437; ` +
        "TBD-382 shipped exactly this 'for consistency').\n" +
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
    // Guards the guard. If `walk` silently returns nothing, every assertion
    // in this file passes vacuously while the gate reports green.
    //
    // ⚠ The coverage claim is only non-circular because `walk` starts at the
    // frontend root and EXCLUDES: the chart files are found by content across
    // the whole tree rather than inside a hand-listed subset of it.
    //
    // ⚠ Since TBD-437 no app file sets isAnimationActive at all, so a
    // tree-wide "the parse saw at least one attribute" check would now be
    // permanently red. That the visitor sees the attribute is proven on
    // synthetic source in the next test instead.
    const chartRel = scanned
      .filter((f) => /from\s+["']recharts["']/.test(sources.get(f)!))
      .map(rel);

    // The surfaces TBD-428 and TBD-437 were filed against, named so a
    // refactor that moves them out of the walk fails here rather than
    // silently shrinking coverage.
    for (const required of [
      "components/dashboard/widgets/BudgetBarsWidget.tsx",
      "components/dashboard/widgets/ForecastBarsWidget.tsx",
      "app/dashboard/page.tsx",
      "app/budgets/BudgetOverviewChart.tsx",
      "app/forecast-plans/ForecastPlanChart.tsx",
      "components/reports/widgets/LineWidgetChart.tsx",
      "components/reports/widgets/AreaWidgetChart.tsx",
      "components/reports/widgets/PieWidgetChart.tsx",
      "components/reports/widgets/SparklineWidgetChart.tsx",
      "components/reports/widgets/BarWidgetChart.tsx",
      "components/scenarios/ProjectionChart.tsx",
      "components/scenarios/ComparisonView.tsx",
    ]) {
      expect(chartRel).toContain(required);
    }
  });

  it("a comment or string cannot hide a violation from the parse, and every verdict is reachable", () => {
    // Direct regression test for the defect that killed the regex version:
    // `/admin/*` in a line comment opened a fake block comment and blanked
    // 325 lines of app/admin/page.tsx. A parse cannot do this, and this test
    // is what proves the property rather than asserting the parser exists.
    const hostile = [
      "// Catalog of /admin/* sub-pages. isAnimationActive={true} in prose.",
      "const doc = \"pass isAnimationActive={true} to force it\";",
      "/* isAnimationActive={false} inside a block comment */",
      "export const A = () => <Bar isAnimationActive={false} />;",
      "export const B = () => <Bar />;",
      "export const C = () => <Bar isAnimationActive />;",
      "export const D = () => <Bar isAnimationActive={on} />;",
      'export const E = () => <Bar isAnimationActive="auto" />;',
      "const props = { isAnimationActive: false };",
    ].join("\n");

    const got = inspectSource(parse("hostile.tsx", hostile), "hostile.tsx").map(
      (o) => `${o.line}:${o.kind}`,
    );
    expect(got).toEqual(["4:literal-false", "6:literal-true", "7:computed", "9:literal-false"]);
  });
});

describe("TBD-437: every recharts mark sets the house animation duration", () => {
  it("the scan resolves marks by import, including aliased and namespaced ones", () => {
    // Guards the guard: an import-resolution bug that finds nothing would
    // make the duration assertion below pass vacuously.
    expect(rechartsMarks.length).toBeGreaterThanOrEqual(20);

    const src = [
      'import { Bar as RBar } from "recharts";',
      'import * as R from "recharts";',
      'import { Bar } from "./not-recharts";',
      "export const A = () => <><RBar /><R.Line /><Bar /></>;",
    ].join("\n");
    const { elements } = importedElements(parse("probe.tsx", src), "probe.tsx", isRecharts);
    expect(elements.map((e) => e.imported)).toEqual(["Bar", "Line"]);
  });

  it('no file dynamically imports "recharts" past the scan', () => {
    const bad = importScans.flatMap((s) =>
      (s.recharts?.dynamicImports ?? []).map((l) => `${rel(s.file)}:${l}`),
    );
    expect(bad, 'import("recharts") hides marks from this gate.\n' + bad.join("\n")).toEqual([]);
  });

  it(`every Bar / Line / Area / Pie sets animationDuration={${HOUSE_DURATION}}`, () => {
    const wrong: string[] = [];
    const missing: Record<string, string[]> = {};
    for (const m of rechartsMarks) {
      const where = `${rel(m.file)}:${m.line} <${m.imported}>`;
      const { present, value } = literalAttr(m.attrs, "animationDuration");
      if (!present) (missing[rel(m.file)] ??= []).push(where);
      else if (!value || !ts.isNumericLiteral(value) || Number(value.text) !== HOUSE_DURATION) {
        wrong.push(where);
      }
    }

    expect(
      wrong,
      `animationDuration must be the literal ${HOUSE_DURATION} (the house value), ` +
        "not computed and not overridable by a later spread. A mark that " +
        "genuinely needs a different duration is a design call: record it " +
        "here with a reason.\n" +
        wrong.join("\n"),
    ).toEqual([]);

    const counts = Object.fromEntries(
      Object.entries(missing)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([f, sites]) => [f, sites.length]),
    );
    expect(
      counts,
      "A recharts mark with no animationDuration runs recharts' 1500ms " +
        `default. Set animationDuration={${HOUSE_DURATION}}. If you fixed a ` +
        "pre-existing one, lower NO_DURATION_BASELINE to match.\n" +
        Object.values(missing).flat().join("\n"),
    ).toEqual(NO_DURATION_BASELINE);
  });
});

describe("TBD-437: every @nivo element keeps animation off", () => {
  it("the scan resolves nivo elements by import, including aliased ones", () => {
    expect(nivoElements.map((e) => rel(e.file))).toContain(
      "components/reports/widgets/SankeyWidgetChart.tsx",
    );

    const src = [
      'import { ResponsiveSankey as Flow } from "@nivo/sankey";',
      'import * as N from "@nivo/core";',
      'import { ResponsiveSankey } from "./elsewhere";',
      "export const A = () => <><Flow /><N.Thing /><ResponsiveSankey /></>;",
    ].join("\n");
    const { elements } = importedElements(parse("probe.tsx", src), "probe.tsx", isNivo);
    expect(elements.map((e) => e.imported)).toEqual(["ResponsiveSankey", "Thing"]);
  });

  it('no file dynamically imports "@nivo/*" past the scan', () => {
    const bad = importScans.flatMap((s) =>
      (s.nivo?.dynamicImports ?? []).map((l) => `${rel(s.file)}:${l}`),
    );
    expect(bad, 'import("@nivo/...") hides elements from this gate.\n' + bad.join("\n")).toEqual(
      [],
    );
  });

  it("every element imported from @nivo/* sets a literal animate={false}", () => {
    const bad = nivoElements
      .filter((e) => literalAttr(e.attrs, "animate").value?.kind !== ts.SyntaxKind.FalseKeyword)
      .map((e) => `${rel(e.file)}:${e.line} <${e.imported}> from ${e.module}`);

    expect(
      bad,
      "@nivo/core@0.99 animates through react-spring with `immediate: !animate` " +
        "and never reads prefers-reduced-motion, so animate={true} (or nivo's " +
        "default, which is true) ships motion to users who opted out. Keep " +
        "animate={false}; converging nivo with the recharts widgets needs a " +
        "reduced-motion gate first (TBD-437).\n" +
        bad.join("\n"),
    ).toEqual([]);
  });
});
