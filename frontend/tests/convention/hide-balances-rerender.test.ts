/**
 * Every component file that formats money subscribes to "Hide balances"
 * (TBD-527, fence F4).
 *
 * The formatters read the flag themselves, so a figure is masked on its NEXT
 * render. What they cannot do is cause that render: a component that formats
 * money without `useMoney` / `useOrgCurrency` / `useWidgetFormat` /
 * `useBalancesHidden` keeps painting the old figure after the toggle, until
 * something unrelated re-renders it. This is the ratchet that stops the next
 * money component shipping without one.
 *
 * ⚠ WHAT THIS CAN AND CANNOT SEE. It is parsed (a mention in a comment or a
 * string does not count, either way) and FILE-level: a file with the hook in
 * one component and a `React.memo` child formatting money in another passes.
 * It sees `formatMoney` / `formatAmount` / `formatMeasureValue` referenced
 * anywhere (called or passed as a formatter) and `money(...)` calls. It does
 * NOT see a `.ts` helper that formats on a component's behalf, nor money built
 * by hand (`toFixed`). The page sweep (`tests/app/hide-balances-sweep.test.tsx`)
 * is the behavioural fence for those.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { globSync } from "glob";
import ts from "typescript";

const ROOT = join(__dirname, "..", "..");

const FORMATTERS = new Set(["formatMoney", "formatAmount", "formatMeasureValue"]);
const HOOKS = new Set(["useMoney", "useOrgCurrency", "useBalancesHidden", "useWidgetFormat"]);

export function scan(source: string): { formatsMoney: boolean; subscribes: boolean } {
  const sf = ts.createSourceFile("x.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let formatsMoney = false;
  let subscribes = false;
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node)) return;
    if (ts.isIdentifier(node) && FORMATTERS.has(node.text)) {
      // A declaration of the name is not a use of it.
      const p = node.parent;
      const declared = (ts.isFunctionDeclaration(p) || ts.isVariableDeclaration(p)) && p.name === node;
      if (!declared) formatsMoney = true;
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
      if (node.expression.text === "money") formatsMoney = true;
      if (HOOKS.has(node.expression.text)) subscribes = true;
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return { formatsMoney, subscribes };
}

describe("F4: money components re-render on Hide balances", () => {
  it("the scanner reads code, not comments or strings", () => {
    expect(scan(`// formatMoney(x) useMoney()\nconst s = "formatAmount(1)";`)).toEqual({
      formatsMoney: false,
      subscribes: false,
    });
    expect(scan(`export default function A(){ return <b>{formatMoney(1)}</b>; }`)).toEqual({
      formatsMoney: true,
      subscribes: false,
    });
    expect(scan(`function A({ money }){ useBalancesHidden(); return <i>{money(2)}</i>; }`)).toEqual({
      formatsMoney: true,
      subscribes: true,
    });
    expect(scan(`<YAxis tickFormatter={formatAmount} />`).formatsMoney).toBe(true);
  });

  it("every .tsx that formats money calls a subscribing hook", () => {
    const files = globSync("{app,components,lib}/**/*.tsx", { cwd: ROOT });
    expect(files.length).toBeGreaterThan(200);

    const moneyFiles = files.filter((rel) => scan(readFileSync(join(ROOT, rel), "utf8")).formatsMoney);
    // Anti-vacuity: 49 files format money at the time of writing. A matcher
    // that drifted to matching nothing would otherwise pass forever.
    expect(moneyFiles.length).toBeGreaterThanOrEqual(45);

    const offenders = moneyFiles.filter(
      (rel) => !scan(readFileSync(join(ROOT, rel), "utf8")).subscribes,
    );
    expect(
      offenders,
      "these format money but will not repaint when Hide balances flips; add `useBalancesHidden();` " +
        "(from @/lib/hooks/use-org-currency) or use useMoney()",
    ).toEqual([]);
  });
});
