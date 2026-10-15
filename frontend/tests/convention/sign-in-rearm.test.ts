import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-623 F11: the tab-identity pin is re-armed ONLY by a deliberate sign-in.
 *
 * ⚠ THIS PARSES (TS compiler API), it does not grep: a mention in a comment or
 * string must not count, and an aliased or namespace import must.
 *
 * ## What this cannot see (the ceiling)
 * - dynamic `import()` / `require()` destructuring, `api["sign" + "In..."]`;
 * - a re-export barrel that renames the function under another module path
 *   (a bare `export { signInWithAccessToken } from` IS counted as a reference);
 * - whether each of the four sites is the right moment to sign in.
 */
const ROOT = path.resolve(__dirname, "..", "..");
const NAME = "signInWithAccessToken";
const API = "lib/api.ts";
const SITES = [
  "app/auth/google/callback/page.tsx",
  "app/mfa-verify/page.tsx",
  "components/auth/AcceptInviteBody.tsx",
  "components/auth/AuthProvider.tsx",
];
const SKIP = new Set(["node_modules", ".next", "out", "out-apex", "public", "coverage", "tests", "scripts"]);

function walk(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const e of readdirSync(dir)) {
    if (e.startsWith(".") || SKIP.has(e)) continue;
    const full = path.join(dir, e);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (/\.(tsx?|jsx?)$/.test(full)) out.push(full);
  }
  return out;
}

function parse(file: string) {
  return ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

/** Count references to the re-arm in one file: aliases of the named import,
 *  property access on a namespace import, and any `.signInWithAccessToken`. */
function references(sf: ts.SourceFile): number {
  const aliases = new Set<string>();
  const visitImports = (n: ts.Node): void => {
    if (ts.isImportDeclaration(n) && n.importClause?.namedBindings && ts.isNamedImports(n.importClause.namedBindings)) {
      for (const el of n.importClause.namedBindings.elements) {
        if ((el.propertyName ?? el.name).text === NAME) aliases.add(el.name.text);
      }
    }
    ts.forEachChild(n, visitImports);
  };
  visitImports(sf);
  let count = 0;
  const visit = (n: ts.Node): void => {
    if (ts.isImportSpecifier(n)) return; // the import itself is not a use
    if (ts.isIdentifier(n) && aliases.has(n.text)) {
      const p = n.parent;
      if (!(ts.isPropertyAccessExpression(p) && p.name === n)) count++;
    }
    if (ts.isPropertyAccessExpression(n) && n.name.text === NAME) count++;
    if (ts.isElementAccessExpression(n) && ts.isStringLiteralLike(n.argumentExpression) && n.argumentExpression.text === NAME) count++;
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return count;
}

describe("TBD-623 F11: the pin is re-armed only by the four sign-in sites", () => {
  it("callers of signInWithAccessToken in non-test source are exactly the four sign-in sites, once each", () => {
    const found: Record<string, number> = {};
    for (const dir of ["app", "components", "lib"]) {
      for (const f of walk(path.join(ROOT, dir))) {
        const rel = path.relative(ROOT, f).split(path.sep).join("/");
        if (rel === API) continue;
        const n = references(parse(f));
        if (n > 0) found[rel] = n;
      }
    }
    expect(found).toEqual(Object.fromEntries(SITES.map((s) => [s, 1])));
  });

  it("api.ts assigns pinnedIdentity = null in exactly one place", () => {
    const sf = parse(path.join(ROOT, API));
    let assigns = 0;
    const visit = (n: ts.Node): void => {
      if (
        ts.isBinaryExpression(n) &&
        n.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
        ts.isIdentifier(n.left) &&
        n.left.text === "pinnedIdentity" &&
        n.right.kind === ts.SyntaxKind.NullKeyword
      ) {
        assigns++;
      }
      ts.forEachChild(n, visit);
    };
    visit(sf);
    expect(assigns).toBe(1);
  });
});
