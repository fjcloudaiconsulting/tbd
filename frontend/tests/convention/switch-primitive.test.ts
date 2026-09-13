import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-323: every switch in the app is the shared primitive.
 *
 * ⚠ THIS PARSES. A grep can be satisfied (or tripped) by a comment; a
 * `role="switch"` in prose must not count, and `role={"switch"}` in JSX must.
 *
 * ## What this cannot see (the ceiling)
 * - spread props (`{...{ role: "switch" }}`) and `React.createElement`;
 * - `aria-pressed` on/off buttons, which cannot be told apart from legitimate
 *   toggle buttons syntactically;
 * - a `label` computed by a helper call such as `stateLabel(checked)` (C3
 *   catches only an inline conditional). The per-site fences in the site test
 *   files are what catch a state-dependent name at today's call sites.
 */

const FRONTEND_ROOT = path.resolve(__dirname, "..", "..");
const PRIMITIVE = "components/ui/Switch.tsx";

const SKIP_DIRS = new Set([
  "node_modules",
  ".next",
  "out",
  "out-apex",
  "public",
  "coverage",
  "tests",
  "scripts",
]);

function walk(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry.startsWith(".") || SKIP_DIRS.has(entry)) continue;
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (full.endsWith(".tsx") || full.endsWith(".ts")) out.push(full);
  }
  return out;
}

/** Static string value of a JSX attribute, or undefined when not static. */
function staticValue(attr: ts.JsxAttribute): string | undefined {
  const init = attr.initializer;
  if (!init) return undefined;
  if (ts.isStringLiteral(init)) return init.text;
  if (ts.isJsxExpression(init) && init.expression) {
    const e = init.expression;
    if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return e.text;
  }
  return undefined;
}

type Hit = { file: string; line: number; tag: string };

const ALLOWED_CHECKED_ROLES = new Set([
  "radio",
  "checkbox",
  "menuitemcheckbox",
  "menuitemradio",
  "option",
  "treeitem",
]);

function scan(file: string, src: string) {
  const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const switches: Hit[] = [];
  const bareChecked: Hit[] = [];
  const conditionalLabels: Hit[] = [];
  const switchTags: Hit[] = [];
  const at = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;

  const visit = (n: ts.Node): void => {
    if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
      const tag = n.tagName.getText(sf);
      const attrs = n.attributes.properties.filter(ts.isJsxAttribute);
      const byName = (name: string) => attrs.find((a) => a.name.getText(sf) === name);
      const role = byName("role");
      const roleValue = role ? staticValue(role) : undefined;
      if (roleValue === "switch") switches.push({ file, line: at(n), tag });
      if (byName("aria-checked") && tag !== "input" && !(roleValue && ALLOWED_CHECKED_ROLES.has(roleValue)) && roleValue !== "switch") {
        bareChecked.push({ file, line: at(n), tag });
      }
      if (tag === "Switch") {
        switchTags.push({ file, line: at(n), tag });
        const label = byName("label");
        let conditional = false;
        const probe = (x: ts.Node): void => {
          if (ts.isConditionalExpression(x)) conditional = true;
          if (
            ts.isBinaryExpression(x) &&
            [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken].includes(
              x.operatorToken.kind,
            )
          ) {
            conditional = true;
          }
          ts.forEachChild(x, probe);
        };
        if (label) probe(label);
        if (conditional) conditionalLabels.push({ file, line: at(n), tag });
      }
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return { switches, bareChecked, conditionalLabels, switchTags };
}

const scanned = walk(FRONTEND_ROOT).map((f) => {
  const rel = path.relative(FRONTEND_ROOT, f);
  return { rel, ...scan(rel, readFileSync(f, "utf8")) };
});

const fmt = (h: Hit) => `${h.file}:${h.line} <${h.tag}>`;

describe("TBD-323: the Switch primitive is the only switch", () => {
  it("C1 fence: role=\"switch\" appears exactly once, in components/ui/Switch.tsx", () => {
    // Kills: a seventh hand copy (in any JSX form), and the primitive losing
    // its role. "Exactly once, in the primitive" is the anti-vacuity half: a
    // walk that stops seeing Switch.tsx goes RED rather than green.
    const all = scanned.flatMap((s) => s.switches);
    const outside = all.filter((h) => h.file !== PRIMITIVE);
    expect(
      outside.map(fmt),
      'Hand-rolled role="switch". Use <Switch> from @/components/ui/Switch.',
    ).toEqual([]);
    expect(all.filter((h) => h.file === PRIMITIVE)).toHaveLength(1);
  });

  it("C2 fence: aria-checked appears only on <input>, an allowed checked role, or the primitive", () => {
    // Kills: a hand-rolled switch that drops role="switch" to dodge C1 but
    // keeps aria-checked on a bare <button>.
    const offenders = scanned.flatMap((s) => s.bareChecked);
    expect(
      offenders.map(fmt),
      "aria-checked without a role that supports it. A binary setting is <Switch>.",
    ).toEqual([]);
  });

  it("C3 guard: a <Switch label> is never an inline conditional", () => {
    // Kills: `label={checked ? "Disable X" : "Enable X"}` at a new call site.
    // ⚠ Guard ceiling: a helper call hides the conditional (see file header).
    const offenders = scanned.flatMap((s) => s.conditionalLabels);
    expect(offenders.map(fmt), "The accessible name must not change with state.").toEqual([]);
    // Anti-vacuity: the guard is only meaningful if it sees real call sites.
    // Parsed JSX tags, not a text search.
    const callSites = new Set(scanned.filter((s) => s.switchTags.length > 0).map((s) => s.rel));
    expect([...callSites].sort()).toEqual(
      expect.arrayContaining([
        "app/settings/notifications/page.tsx",
        "components/settings/PlanningToolsCard.tsx",
        "components/settings/SchedulerSettingsCard.tsx",
        "components/settings/SmartRulesSection.tsx",
      ]),
    );
  });
});

describe("TBD-323: anti-vacuity", () => {
  it("C4: the walk reaches the primitive and every migrated site", () => {
    const rels = scanned.map((s) => s.rel);
    for (const required of [
      PRIMITIVE,
      "components/settings/SchedulerSettingsCard.tsx",
      "components/settings/SmartRulesSection.tsx",
      "components/settings/PlanningToolsCard.tsx",
      "app/settings/notifications/page.tsx",
    ]) {
      expect(rels).toContain(required);
    }
  });

  it("C4b: a comment cannot trip the parse; JSX in any static form can", () => {
    const hostile = [
      '// role="switch" in a line comment',
      '/* <button role="switch" aria-checked={x} /> in a block comment */',
      'const prose = \'role="switch"\';',
      'export const A = () => <button role={"switch"} aria-checked={true} />;',
      "export const B = () => <button role={`switch`} />;",
      "export const C = () => <button aria-checked={false} />;",
      "export const D = () => <input type=\"checkbox\" aria-checked=\"mixed\" />;",
      'export const E = () => <div role="radio" aria-checked={true} />;',
      'export const F = () => <Switch label={on ? "Disable X" : "Enable X"} />;',
      'export const G = () => <Switch label={`${title} email notifications`} />;',
    ].join("\n");
    const r = scan("hostile.tsx", hostile);
    expect(r.switches.map((h) => h.line)).toEqual([4, 5]);
    expect(r.bareChecked.map((h) => h.line)).toEqual([6]);
    expect(r.conditionalLabels.map((h) => h.line)).toEqual([9]);
  });
});
