import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-428 / TBD-437: structural companion to
 * ``tests/components/charts/reduced-motion.test.tsx`` and
 * ``tests/components/charts/line-geometry.test.tsx``.
 *
 * The behavioural fences prove recharts' ``"auto"`` default honours
 * ``prefers-reduced-motion`` and that the app's Lines draw whole strokes.
 * What they CANNOT see is a call site: they render a handful of charts, not
 * every recharts surface in the app. So a contributor writing
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
 * * **Prop absent**: resolves to ``"auto"``, which skips the animation when
 *   the user asked for reduced motion. Correct for ``Bar``, ``Area`` and
 *   ``Pie``. Not visited.
 * * ``isAnimationActive="auto"``: the same thing spelled out. Allowed.
 * * ``isAnimationActive={true}`` or the bare shorthand: **rejected.** Both
 *   mean literal ``true``, which overrides ``"auto"`` and animates regardless
 *   of the user's preference.
 * * ``isAnimationActive={false}``: **rejected since TBD-437, except at the
 *   exact sites in ``HARD_OFF_EXCEPTIONS``.** TBD-382 turned animation off
 *   for every report widget "for consistency", removing motion for users who
 *   never opted out, and TBD-428's version of this gate allowed it. For Bar,
 *   Area and Pie, ``"auto"`` handles reduced motion, so ``false`` only
 *   subtracts. ⚠ That is NOT true of recharts 3.8.1's ``<Line>``: it gates its
 *   stroke-dash override on the raw prop, so under reduced motion ``"auto"``
 *   draws a stale, partial stroke (line-geometry.test.tsx). The four app Lines
 *   therefore stay hard-off until the recharts bump (TBD-528), and the
 *   version tripwire below fails the day recharts moves.
 * * A **computed** value (``{shouldAnimate}``, a shorthand
 *   ``{ isAnimationActive }``, a non-literal computed key spread onto an
 *   element): rejected with a different message. It is refused rather than
 *   waved through because a computed gate is exactly the change that needs a
 *   matching behavioural fence.
 *
 * ## Two further fences (TBD-437)
 *
 * * **Every recharts ``Bar`` / ``Line`` / ``Area`` / ``Pie`` sets
 *   ``animationDuration={220}``**, the ``chart-entrance`` motion token in
 *   ``docs/design/DESIGN.json``. Matched by IMPORT from ``"recharts"`` or a
 *   ``recharts/*`` deep import, so an alias or a namespace import cannot hide
 *   one. Without it a mark runs recharts' 1500ms default. The hard-off Lines
 *   are exempt; marks that predate this fence and set no duration are listed
 *   in ``NO_DURATION_BASELINE`` with exact counts.
 * * **Every nivo CHART component sets literal ``animate={false}``**, matched
 *   by IMPORT, not by the name at the call site. ``@nivo/core@0.99`` passes
 *   ``immediate: !animate`` to react-spring and never reads
 *   ``prefers-reduced-motion``, so enabling it would ship motion to users who
 *   opted out. See SankeyWidgetChart.tsx.
 *
 * Both import-resolved scans also refuse the shapes they cannot follow: a
 * dynamic ``import(...)``, a ``require(...)``, an ``import x = require(...)``
 * and an ``export ... from`` re-export of either library.
 */

const FRONTEND_ROOT = path.resolve(__dirname, "..", "..");

/** Skipped at the frontend ROOT only, together with every dot-entry there.
 *
 *  ⚠ An exclusion list, not an inclusion list, and that is load-bearing.
 *  Review found the earlier ``ROOTS = ["app", "components"]`` form made the
 *  companion coverage assertion a tautology: the chart files were *derived
 *  from* the scanned set, so "every chart file is scanned" could not fail.
 *
 *  ⚠ Root-level ONLY (TBD-437). Matching these names at any depth would
 *  silently drop a future ``components/x/tests/`` or ``lib/public/`` from the
 *  scan with every test green. */
const ROOT_SKIP = new Set(["node_modules", "out", "out-apex", "public", "coverage", "tests"]);
/** tsconfig has ``allowJs: true``, so app code may be any of these. */
const EXTENSIONS: Record<string, ts.ScriptKind> = {
  ".tsx": ts.ScriptKind.TSX,
  ".ts": ts.ScriptKind.TS,
  ".jsx": ts.ScriptKind.JSX,
  ".js": ts.ScriptKind.JS,
  ".mjs": ts.ScriptKind.JS,
};

const skipEntry = (entry: string, atRoot: boolean) =>
  atRoot && (entry.startsWith(".") || ROOT_SKIP.has(entry));

function walk(dir: string, atRoot = true): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (skipEntry(entry, atRoot)) continue;
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full, false));
    else if (path.extname(full) in EXTENSIONS && !full.endsWith(".d.ts")) out.push(full);
  }
  return out;
}

const parse = (file: string, source: string) =>
  ts.createSourceFile(
    file,
    source,
    ts.ScriptTarget.Latest,
    true,
    EXTENSIONS[path.extname(file)] ?? ts.ScriptKind.TSX,
  );
const lineOf = (sf: ts.SourceFile, n: ts.Node) =>
  sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;

type Offence = {
  file: string;
  line: number;
  kind: "literal-true" | "literal-false" | "computed";
  /** Start of the JSX element carrying the attribute, when there is one. */
  elementPos?: number;
};

const isStaticName = (n: ts.Node): n is ts.StringLiteral | ts.NoSubstitutionTemplateLiteral =>
  ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n);

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
  const push = (node: ts.Node, kind: Offence["kind"] | null, elementPos?: number) => {
    if (kind) found.push({ file, line: lineOf(sf, node), kind, elementPos });
  };

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
      push(node, kind, node.parent.parent.getStart(sf));
    }

    // `const chartProps = { isAnimationActive: false }` (or a quoted or
    // static computed key), later spread onto a chart. The property name is
    // specific enough that this cannot collide with unrelated code.
    if (ts.isPropertyAssignment(node)) {
      const name = node.name;
      const key =
        ts.isIdentifier(name) || ts.isStringLiteral(name)
          ? name.text
          : ts.isComputedPropertyName(name) && isStaticName(name.expression)
            ? name.expression.text
            : undefined;
      if (key === "isAnimationActive") push(node, kindOf(node.initializer));
      else if (
        ts.isComputedPropertyName(name) &&
        !isStaticName(name.expression) &&
        ts.isObjectLiteralExpression(node.parent) &&
        ts.isJsxSpreadAttribute(node.parent.parent)
      ) {
        // `<Bar {...{ [k]: false }} />`: the key cannot be read, so it may be
        // isAnimationActive.
        push(node, "computed");
      }
    }

    // `const isAnimationActive = on; <Bar {...{ isAnimationActive }} />`
    if (ts.isShorthandPropertyAssignment(node) && node.name.text === "isAnimationActive") {
      push(node, "computed");
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
  pos: number;
  module: string;
  /** The exported name (`Bar`), regardless of any local alias. */
  imported: string;
  attrs: ts.JsxAttributes;
};

function importedElements(
  sf: ts.SourceFile,
  file: string,
  matchModule: (m: string) => boolean,
): { elements: ImportedElement[]; escapes: string[] } {
  // local name -> binding; imported === "*" for a namespace import.
  const bindings = new Map<string, { module: string; imported: string }>();
  const escapes: string[] = [];
  for (const stmt of sf.statements) {
    if (ts.isImportDeclaration(stmt) && ts.isStringLiteral(stmt.moduleSpecifier)) {
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
    } else if (
      ts.isExportDeclaration(stmt) &&
      stmt.moduleSpecifier &&
      ts.isStringLiteral(stmt.moduleSpecifier) &&
      matchModule(stmt.moduleSpecifier.text)
    ) {
      escapes.push(`${lineOf(sf, stmt)} re-export from "${stmt.moduleSpecifier.text}"`);
    } else if (
      ts.isImportEqualsDeclaration(stmt) &&
      ts.isExternalModuleReference(stmt.moduleReference) &&
      ts.isStringLiteral(stmt.moduleReference.expression) &&
      matchModule(stmt.moduleReference.expression.text)
    ) {
      escapes.push(`${lineOf(sf, stmt)} import = require("${stmt.moduleReference.expression.text}")`);
    }
  }

  const elements: ImportedElement[] = [];
  const visit = (node: ts.Node): void => {
    if (
      ts.isCallExpression(node) &&
      (node.expression.kind === ts.SyntaxKind.ImportKeyword ||
        (ts.isIdentifier(node.expression) && node.expression.text === "require")) &&
      node.arguments[0] &&
      ts.isStringLiteral(node.arguments[0]) &&
      matchModule(node.arguments[0].text)
    ) {
      escapes.push(`${lineOf(sf, node)} dynamic ${node.expression.getText(sf)}("${node.arguments[0].text}")`);
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
      if (hit) {
        elements.push({
          file,
          line: lineOf(sf, node),
          pos: node.getStart(sf),
          ...hit,
          attrs: node.attributes,
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return { elements, escapes };
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

/** docs/design/DESIGN.json -> tokens.motion "chart-entrance" (TBD-437). */
const CHART_ENTRANCE_MS = 220;
const TOKEN_CITE = 'the "chart-entrance" motion token (220ms) in docs/design/DESIGN.json';

function durationVerdict(attrs: ts.JsxAttributes): "ok" | "wrong" | "missing" {
  const { present, value } = literalAttr(attrs, "animationDuration");
  if (!present) return "missing";
  return value && ts.isNumericLiteral(value) && Number(value.text) === CHART_ENTRANCE_MS
    ? "ok"
    : "wrong";
}

const isRecharts = (m: string) => m === "recharts" || m.startsWith("recharts/");
const isNivo = (m: string) => m.startsWith("@nivo/");

const RECHARTS_MARKS = new Set(["Bar", "Line", "Area", "Pie"]);

/**
 * The ONLY recharts marks allowed ``isAnimationActive={false}``: the app's
 * four ``<Line>`` sites, hard-off because recharts 3.8.1 draws a stale,
 * partial stroke under reduced motion when a Line is left on ``"auto"``
 * (TBD-528; fenced behaviourally by line-geometry.test.tsx). Compared EXACTLY,
 * in both directions: a fifth site fails, and so does removing one of these.
 */
const HARD_OFF_EXCEPTIONS = [
  "components/reports/widgets/LineWidgetChart.tsx <Line>",
  "components/reports/widgets/SparklineWidgetChart.tsx <Line>",
  "components/scenarios/ComparisonView.tsx <Line>",
  "components/scenarios/ProjectionChart.tsx <Line>",
];
const RECHARTS_WITH_LINE_DEFECT = "3.8.1";

/**
 * Marks that predate TBD-437, set no ``animationDuration``, and so run
 * recharts' 1500ms default. The architect ruling named the report and
 * scenario marks only, so these were recorded rather than silently re-timed.
 * EXACT counts per file: a new duration-less mark in one of these files
 * fails, and so does fixing one without lowering its count here.
 */
const NO_DURATION_BASELINE: Record<string, number> = {
  "app/dashboard/page.tsx": 1,
  "components/dashboard/widgets/CreditUtilizationBar.tsx": 2,
  "components/dashboard/widgets/SpendingDonutWidget.tsx": 1,
};

/**
 * Every recharts Bar/Line/Area/Pie the scan resolves, per file. EXACT, so a
 * refactor that makes marks silently drop out of import resolution (and so
 * out of every rule above) fails here instead of shrinking coverage.
 */
const RECHARTS_MARK_COUNTS: Record<string, number> = {
  "app/budgets/BudgetOverviewChart.tsx": 3,
  "app/dashboard/page.tsx": 5,
  "app/forecast-plans/ForecastPlanChart.tsx": 2,
  "components/dashboard/widgets/BudgetBarsWidget.tsx": 2,
  "components/dashboard/widgets/CreditUtilizationBar.tsx": 2,
  "components/dashboard/widgets/ForecastBarsWidget.tsx": 2,
  "components/dashboard/widgets/SpendingDonutWidget.tsx": 1,
  "components/reports/widgets/AreaWidgetChart.tsx": 1,
  "components/reports/widgets/BarWidgetChart.tsx": 2,
  "components/reports/widgets/LineWidgetChart.tsx": 1,
  "components/reports/widgets/PieWidgetChart.tsx": 1,
  "components/reports/widgets/SparklineWidgetChart.tsx": 1,
  "components/scenarios/ComparisonView.tsx": 1,
  "components/scenarios/ProjectionChart.tsx": 2,
};

/** nivo packages that ship building blocks, not charts with an ``animate``
 *  prop (e.g. ``BasicTooltip`` from ``@nivo/tooltip``). */
const NIVO_NON_CHART_PACKAGES = new Set([
  "annotations",
  "arcs",
  "axes",
  "colors",
  "core",
  "grid",
  "legends",
  "scales",
  "text",
  "theming",
  "tooltip",
  "voronoi",
]);

/** nivo names its charts after the package: ``@nivo/sankey`` exports
 *  ``Sankey`` and ``ResponsiveSankey``; ``@nivo/bar`` adds ``BarCanvas`` and
 *  ``ResponsiveBarCanvas``. */
function nivoKind(module: string, imported: string): "chart" | "non-chart" | "unclassified" {
  const pkg = module.slice("@nivo/".length).split("/")[0];
  if (NIVO_NON_CHART_PACKAGES.has(pkg)) return "non-chart";
  const base = pkg
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join("");
  const names = [base, `${base}Canvas`].flatMap((n) => [n, `Responsive${n}`]);
  return names.includes(imported) ? "chart" : "unclassified";
}

const scanned = walk(FRONTEND_ROOT);
// Read once at collection time. Every test below is then pure computation, so
// none of them can hit the per-test timeout on a loaded runner by re-reading
// the tree (the "reaches the chart surface" check once did, at 5s).
const sources = new Map(scanned.map((f) => [f, readFileSync(f, "utf8")]));
const rel = (f: string) => path.relative(FRONTEND_ROOT, f);

const fileScans = scanned.map((file) => {
  const source = sources.get(file)!;
  const wantsRecharts = source.includes("recharts");
  const wantsNivo = source.includes("@nivo/");
  // Pre-filter on RAW source. A file that never mentions the prop or the
  // library cannot set the prop on one of its marks; a file that mentions it
  // only in prose is parsed and then found clean.
  if (!wantsRecharts && !wantsNivo && !source.includes("isAnimationActive")) {
    return { file, offences: [], recharts: null, nivo: null };
  }
  const sf = parse(file, source);
  return {
    file,
    offences: inspectSource(sf, file),
    recharts: wantsRecharts ? importedElements(sf, file, isRecharts) : null,
    nivo: wantsNivo ? importedElements(sf, file, isNivo) : null,
  };
});
const offences = fileScans.flatMap((s) => s.offences);
const rechartsMarks = fileScans
  .flatMap((s) => s.recharts?.elements ?? [])
  .filter((e) => RECHARTS_MARKS.has(e.imported));
const nivoElements = fileScans.flatMap((s) => s.nivo?.elements ?? []);

const markLabel = (m: ImportedElement) => `${rel(m.file)} <${m.imported}>`;
const markAt = (file: string, pos?: number) =>
  rechartsMarks.find((m) => m.file === file && m.pos === pos);

function hostile(lines: string[]) {
  const sf = parse("hostile.tsx", lines.join("\n"));
  return { sf, offences: inspectSource(sf, "hostile.tsx") };
}

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

  it("TBD-437: isAnimationActive={false} appears on exactly the TBD-528 Line sites", () => {
    const falses = offences.filter((o) => o.kind === "literal-false");
    const labels = falses.map((o) => {
      const m = markAt(o.file, o.elementPos);
      return m
        ? markLabel(m)
        : `${rel(o.file)}:${o.line} ${o.elementPos === undefined ? "(object literal)" : "(not a recharts mark)"}`;
    });

    expect(
      [...labels].sort(),
      "isAnimationActive={false} removes chart motion for every user, " +
        "including the ones who never asked for reduced motion; for Bar, Area " +
        "and Pie recharts' \"auto\" default already skips it for those who did. " +
        `Drop the prop and set animationDuration={${CHART_ENTRANCE_MS}} (${TOKEN_CITE}). ` +
        "The only exceptions are the four <Line> sites in HARD_OFF_EXCEPTIONS " +
        "(recharts 3.8.1 Line defect, TBD-528). If you removed one, recharts was " +
        "bumped past the defect: delete it from the list too.\n" +
        falses.map((o, i) => `${labels[i]} (line ${o.line})`).join("\n"),
    ).toEqual([...HARD_OFF_EXCEPTIONS].sort());
  });

  it(`TBD-528 tripwire: the hard-off Lines expire when recharts leaves ${RECHARTS_WITH_LINE_DEFECT}`, () => {
    const pkg = createRequire(path.join(FRONTEND_ROOT, "package.json")).resolve(
      "recharts/package.json",
    );
    const version = JSON.parse(readFileSync(pkg, "utf8")).version as string;
    if (HARD_OFF_EXCEPTIONS.length === 0) return;
    expect(
      version,
      `recharts is no longer ${RECHARTS_WITH_LINE_DEFECT}, so the reason for ` +
        "HARD_OFF_EXCEPTIONS may be gone. Re-run " +
        "tests/components/charts/line-geometry.test.tsx with the four Lines on " +
        `"auto" (animationDuration={${CHART_ENTRANCE_MS}}, no isAnimationActive). ` +
        "Since 3.9.0 the dash lives in LineDrawShape and is applied only while " +
        "visibleLength is set; 3.10.1 measured clean. If it is green, delete the " +
        "exceptions and move the Lines to the house duration (TBD-528).",
    ).toBe(RECHARTS_WITH_LINE_DEFECT);
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
    const chartRel = scanned
      .filter((f) => /from\s+["']recharts["']/.test(sources.get(f)!))
      .map(rel);

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

  it("the walk skips build and test directories at the root only, and reads every allowJs extension", () => {
    // Root level: skipped.
    for (const e of ["tests", "node_modules", "public", ".next", ".storybook"]) {
      expect(skipEntry(e, true), e).toBe(true);
    }
    // Any depth below the root: walked. A future `components/x/tests/` or
    // `lib/.generated/` must not drop out of the scan.
    for (const e of ["tests", "node_modules", "public", ".next", "coverage"]) {
      expect(skipEntry(e, false), e).toBe(false);
    }
    expect(skipEntry("components", true)).toBe(false);
    expect(Object.keys(EXTENSIONS).sort()).toEqual([".js", ".jsx", ".mjs", ".ts", ".tsx"]);
    // The root .mjs configs are proof the walk reads JS, not just TS.
    expect(scanned.map(rel)).toContain("eslint.config.mjs");
  });

  it("a comment or string cannot hide a violation from the parse, and every verdict is reachable", () => {
    // Direct regression test for the defect that killed the regex version:
    // `/admin/*` in a line comment opened a fake block comment and blanked
    // 325 lines of app/admin/page.tsx. A parse cannot do this, and this test
    // is what proves the property rather than asserting the parser exists.
    const { offences: got } = hostile([
      "// Catalog of /admin/* sub-pages. isAnimationActive={true} in prose.",
      "const doc = \"pass isAnimationActive={true} to force it\";",
      "/* isAnimationActive={false} inside a block comment */",
      "export const A = () => <Bar isAnimationActive={false} />;",
      "export const B = () => <Bar />;",
      "export const C = () => <Bar isAnimationActive />;",
      "export const D = () => <Bar isAnimationActive={on} />;",
      'export const E = () => <Bar isAnimationActive="auto" />;',
      "const props = { isAnimationActive: false };",
      "const isAnimationActive = on; const s = { isAnimationActive };",
      'const q = { ["isAnimationActive"]: true };',
      "export const F = () => <Bar {...{ [k]: false }} />;",
      "const unrelated = { [k]: false };",
    ]);
    expect(got.map((o) => `${o.line}:${o.kind}`)).toEqual([
      "4:literal-false",
      "6:literal-true",
      "7:computed",
      "9:literal-false",
      "10:computed",
      "11:literal-true",
      "12:computed",
    ]);
  });
});

describe("TBD-437: every recharts mark sets the chart-entrance duration", () => {
  it("the scan resolves exactly the marks it resolved when this fence was written", () => {
    expect(
      Object.fromEntries(
        Object.entries(
          rechartsMarks.reduce<Record<string, number>>((acc, m) => {
            acc[rel(m.file)] = (acc[rel(m.file)] ?? 0) + 1;
            return acc;
          }, {}),
        ).sort(([a], [b]) => a.localeCompare(b)),
      ),
      "The set of recharts Bar/Line/Area/Pie marks the import scan resolves " +
        "changed. If you added or removed a mark, update RECHARTS_MARK_COUNTS. " +
        "If you did not, marks have dropped out of import resolution and every " +
        "rule in this file has silently stopped covering them.",
    ).toEqual(RECHARTS_MARK_COUNTS);
  });

  it("import resolution follows aliases, namespaces and deep imports, and nothing else", () => {
    const src = [
      'import { Bar as RBar } from "recharts";',
      'import * as R from "recharts";',
      'import { Area } from "recharts/es6/cartesian/Area";',
      'import { Bar } from "./not-recharts";',
      "export const A = () => <><RBar /><R.Line /><Area /><Bar /></>;",
    ].join("\n");
    const { elements } = importedElements(parse("probe.tsx", src), "probe.tsx", isRecharts);
    expect(elements.map((e) => e.imported)).toEqual(["Bar", "Line", "Area"]);
  });

  it("the duration verdict cannot be satisfied by a string, a variable or an overriding spread", () => {
    const cases: Array<[string, string]> = [
      ["<Bar animationDuration={220} />", "ok"],
      ["<Bar {...p} animationDuration={220} />", "ok"],
      ["<Bar animationDuration={220} {...p} />", "wrong"],
      ['<Bar animationDuration="220" />', "wrong"],
      ['<Bar animationDuration={"220"} />', "wrong"],
      ["<Bar animationDuration={D} />", "wrong"],
      ["<Bar animationDuration={1500} />", "wrong"],
      ["<Bar />", "missing"],
      ["<Bar {...{ animationDuration: 220 }} />", "missing"],
    ];
    const src = [
      'import { Bar } from "recharts";',
      ...cases.map(([jsx], i) => `export const C${i} = () => ${jsx};`),
    ].join("\n");
    const { elements } = importedElements(parse("probe.tsx", src), "probe.tsx", isRecharts);
    expect(elements.map((e) => durationVerdict(e.attrs))).toEqual(cases.map(([, v]) => v));
  });

  it("no file reaches recharts through a path the scan cannot follow", () => {
    const bad = fileScans.flatMap((s) => (s.recharts?.escapes ?? []).map((e) => `${rel(s.file)}:${e}`));
    expect(
      bad,
      "Dynamic imports, require() and re-exports hand recharts marks to the " +
        "tree where this gate cannot resolve them. Import the marks directly.\n" +
        bad.join("\n"),
    ).toEqual([]);

    const probe = [
      'export { Bar } from "recharts";',
      'export * from "recharts/lib/index";',
      'import R = require("recharts");',
      'const lazy = () => import("recharts");',
      'const req = require("recharts");',
    ].join("\n");
    expect(
      importedElements(parse("probe.ts", probe), "probe.ts", isRecharts).escapes.map(
        (e) => e.split(" ")[0],
      ),
    ).toEqual(["1", "2", "3", "4", "5"]);
  });

  it(`every Bar / Line / Area / Pie sets animationDuration={${CHART_ENTRANCE_MS}}`, () => {
    const hardOff = new Set(HARD_OFF_EXCEPTIONS);
    const wrong: string[] = [];
    const missing: Record<string, string[]> = {};
    for (const m of rechartsMarks) {
      const where = `${rel(m.file)}:${m.line} <${m.imported}>`;
      const off = literalAttr(m.attrs, "isAnimationActive").value?.kind === ts.SyntaxKind.FalseKeyword;
      // The hard-off Lines never animate, so a duration would be dead code.
      // Which marks may be hard-off is policed by the exception test above.
      if (off && hardOff.has(markLabel(m))) continue;
      const verdict = durationVerdict(m.attrs);
      if (verdict === "missing") (missing[rel(m.file)] ??= []).push(where);
      else if (verdict === "wrong") wrong.push(where);
    }

    expect(
      wrong,
      `animationDuration must be the literal ${CHART_ENTRANCE_MS}, ${TOKEN_CITE}, ` +
        "not computed and not overridable by a later spread. A mark that " +
        "genuinely needs a different duration is a design call: add a token " +
        "and record the site here with a reason.\n" +
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
        `default. Set animationDuration={${CHART_ENTRANCE_MS}} (${TOKEN_CITE}). ` +
        "If you fixed a pre-existing one, lower NO_DURATION_BASELINE to match.\n" +
        Object.values(missing).flat().join("\n"),
    ).toEqual(NO_DURATION_BASELINE);
  });
});

describe("TBD-437: every nivo chart keeps animation off", () => {
  it("the scan resolves nivo charts by import, including aliases, and exempts building blocks", () => {
    expect(nivoElements.map((e) => rel(e.file))).toContain(
      "components/reports/widgets/SankeyWidgetChart.tsx",
    );

    const src = [
      'import { ResponsiveSankey as Flow } from "@nivo/sankey";',
      'import * as Bars from "@nivo/bar";',
      'import { BasicTooltip } from "@nivo/tooltip";',
      'import { ResponsiveSankey } from "./elsewhere";',
      "export const A = () => <><Flow /><Bars.ResponsiveBarCanvas /><BasicTooltip /><ResponsiveSankey /></>;",
    ].join("\n");
    const { elements } = importedElements(parse("probe.tsx", src), "probe.tsx", isNivo);
    expect(elements.map((e) => `${e.imported}:${nivoKind(e.module, e.imported)}`)).toEqual([
      "ResponsiveSankey:chart",
      "ResponsiveBarCanvas:chart",
      "BasicTooltip:non-chart",
    ]);
  });

  it("every nivo element in the app is classified as a chart or a building block", () => {
    // The chart rule below is enumerated by nivoKind. This second sweep over
    // ALL nivo elements is what stops a chart that nivoKind does not
    // recognise (say @nivo/geo's `Choropleth`) from escaping it silently.
    const bad = nivoElements
      .filter((e) => nivoKind(e.module, e.imported) === "unclassified")
      .map((e) => `${rel(e.file)}:${e.line} <${e.imported}> from ${e.module}`);
    expect(
      bad,
      "A nivo element is neither a recognised chart nor from a building-block " +
        "package. Decide which it is: extend nivoKind or NIVO_NON_CHART_PACKAGES.\n" +
        bad.join("\n"),
    ).toEqual([]);
  });

  it("no file reaches nivo through a path the scan cannot follow", () => {
    const bad = fileScans.flatMap((s) => (s.nivo?.escapes ?? []).map((e) => `${rel(s.file)}:${e}`));
    expect(bad, "Import nivo charts directly.\n" + bad.join("\n")).toEqual([]);
  });

  it("every nivo chart sets a literal animate={false}", () => {
    const bad = nivoElements
      .filter((e) => nivoKind(e.module, e.imported) === "chart")
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
