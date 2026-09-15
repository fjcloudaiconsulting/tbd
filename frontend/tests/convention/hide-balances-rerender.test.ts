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
 * Two rules:
 *  1. A file that formats money calls a subscribing hook.
 *  2. A formatter inside a `useMemo` / `useCallback` body needs `hidden` or
 *     `money` in its deps, or the memo hands back the stale figure even though
 *     the component re-rendered.
 *
 * ⚠ WHAT THIS CAN AND CANNOT SEE. It is parsed (a mention in a comment or a
 * string does not count, either way). Rule 1 is FILE-level: a file with the
 * hook in one component and a `React.memo` child formatting money in another
 * passes. It sees `formatMoney` / `formatAmount` / `formatMeasureValue` /
 * `maskMoneyText` referenced anywhere (called, passed as a formatter, or
 * imported under an alias) and `money(...)` calls. Rule 2 reads only an
 * inline deps array literal. Neither sees a `.ts` helper that formats on a
 * component's behalf, nor money built by hand (`toFixed`). The page sweep
 * (`tests/app/hide-balances-sweep.test.tsx`) is the behavioural fence for those.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { globSync } from "glob";
import ts from "typescript";

const ROOT = join(__dirname, "..", "..");

const FORMATTERS = new Set(["formatMoney", "formatAmount", "formatMeasureValue", "maskMoneyText"]);
const HOOKS = new Set(["useMoney", "useOrgCurrency", "useBalancesHidden", "useWidgetFormat"]);
const MEMO_HOOKS = new Set(["useMemo", "useCallback"]);
const MEMO_DEPS_OK = new Set(["hidden", "money"]);
const MONEY_CALL = "money";
// Parsing every .tsx is the slow part (TBD-540). Only a formatter name or a
// `money(` call can make scan() report anything (a hook alone flags nothing,
// and an aliased import still spells the original name), so a file whose raw
// text contains none of them cannot change the result.
const CANDIDATE = new RegExp([...FORMATTERS, MONEY_CALL].join("|"));

interface Scan {
  formatsMoney: boolean;
  subscribes: boolean;
  /** 1-based lines of memo hooks that format money without `hidden`/`money` deps. */
  staleMemos: number[];
}

function scan(source: string): Scan {
  const sf = ts.createSourceFile("x.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const result: Scan = { formatsMoney: false, subscribes: false, staleMemos: [] };
  // Local names bound to a formatter, including `formatMoney as fm`.
  const names = new Set(FORMATTERS);

  const isFormatterUse = (node: ts.Node): boolean => {
    if (ts.isIdentifier(node) && names.has(node.text)) {
      const p = node.parent;
      // A declaration of the name is not a use of it.
      return !((ts.isFunctionDeclaration(p) || ts.isVariableDeclaration(p)) && p.name === node);
    }
    return ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === MONEY_CALL;
  };
  const containsFormatter = (node: ts.Node): boolean =>
    isFormatterUse(node) || (ts.forEachChild(node, (c) => (containsFormatter(c) ? true : undefined)) ?? false);

  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node)) {
      const bindings = node.importClause?.namedBindings;
      if (bindings && ts.isNamedImports(bindings)) {
        for (const el of bindings.elements) {
          if (FORMATTERS.has((el.propertyName ?? el.name).text)) {
            names.add(el.name.text);
            result.formatsMoney = true;
          }
        }
      }
      return;
    }
    if (isFormatterUse(node)) result.formatsMoney = true;
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
      const callee = node.expression.text;
      if (HOOKS.has(callee)) result.subscribes = true;
      const [body, deps] = node.arguments;
      if (MEMO_HOOKS.has(callee) && body && deps && ts.isArrayLiteralExpression(deps)) {
        const depsOk = deps.elements.some((d) => ts.isIdentifier(d) && MEMO_DEPS_OK.has(d.text));
        if (!depsOk && containsFormatter(body)) {
          result.staleMemos.push(sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return result;
}

describe("F4: money components re-render on Hide balances", () => {
  it("the scanner reads code, not comments or strings", () => {
    expect(scan(`// formatMoney(x) useMoney()\nconst s = "formatAmount(1)";`)).toEqual({
      formatsMoney: false,
      subscribes: false,
      staleMemos: [],
    });
    expect(scan(`export default function A(){ return <b>{formatMoney(1)}</b>; }`)).toMatchObject({
      formatsMoney: true,
      subscribes: false,
    });
    expect(scan(`function A({ money }){ useBalancesHidden(); return <i>{money(2)}</i>; }`)).toMatchObject({
      formatsMoney: true,
      subscribes: true,
    });
    expect(scan(`<YAxis tickFormatter={formatAmount} />`).formatsMoney).toBe(true);
  });

  it("an aliased import counts as a use", () => {
    expect(scan(`import { formatMoney as fm } from "@/lib/format";\nconst A = () => <b>{fm(1)}</b>;`))
      .toMatchObject({ formatsMoney: true, subscribes: false });
  });

  it("flags a memo that formats money without hidden/money in its deps", () => {
    const src = [
      "function A({ rows, money, hidden }) {",
      "  const a = useMemo(() => rows.map((r) => formatMoney(r.v)), [rows]);",
      "  const b = useMemo(() => rows.map((r) => formatMoney(r.v)), [rows, hidden]);",
      "  const c = useCallback((v) => money(v), [money]);",
      "  const d = useMemo(() => rows.length, [rows]);",
      "  const e = useCallback((t) => maskMoneyText(t), []);",
      "}",
    ].join("\n");
    expect(scan(src).staleMemos).toEqual([2, 6]);
  });

  it("every .tsx that formats money calls a subscribing hook, with no stale memo", () => {
    const files = globSync("{app,components,lib}/**/*.tsx", { cwd: ROOT });
    expect(files.length).toBeGreaterThan(200);

    const scans = files.flatMap((rel) => {
      const source = readFileSync(join(ROOT, rel), "utf8");
      return CANDIDATE.test(source) ? [{ rel, ...scan(source) }] : [];
    });
    const moneyFiles = scans.filter((s) => s.formatsMoney);
    // Strict, like the repo's other ratchets: a drop means the matcher died or
    // a file stopped formatting money; either way, read it and update this.
    expect(moneyFiles.length).toBe(48);

    expect(
      moneyFiles.filter((s) => !s.subscribes).map((s) => s.rel),
      "these format money but will not repaint when Hide balances flips; add `useBalancesHidden();` " +
        "(from @/lib/hooks/use-org-currency) or use useMoney()",
    ).toEqual([]);
    expect(
      scans.flatMap((s) => s.staleMemos.map((line) => `${s.rel}:${line}`)),
      "these memoise a formatted figure without `hidden` or `money` in their deps",
    ).toEqual([]);
  });
});
