import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-624 F1. No server render may act as the refresh cookie's identity: the
 * cookie is shared by every tab, so a server read keyed on it returns whichever
 * account signed in last. PARSES imports (a grep is satisfied by a comment).
 * Kills: re-adding `cookies()` from next/headers or `@/lib/auth-server`.
 * Ceiling: dynamic `import()` with a non-literal specifier, `require`.
 */
const ROOT = path.resolve(__dirname, "..", "..");
const SKIP = new Set(["node_modules", ".next", "out", "out-apex", "public", "coverage"]);

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

function offences(file: string): string[] {
  const sf = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const hits: string[] = [];
  const check = (spec: string, names: string[]) => {
    if (/(^|\/)auth-server$/.test(spec)) hits.push(`imports ${spec}`);
    if (spec === "next/headers" && names.includes("cookies")) hits.push("imports cookies from next/headers");
  };
  const visit = (n: ts.Node): void => {
    if ((ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) && n.moduleSpecifier && ts.isStringLiteral(n.moduleSpecifier)) {
      const names: string[] = [];
      if (ts.isImportDeclaration(n)) {
        const nb = n.importClause?.namedBindings;
        if (nb && ts.isNamedImports(nb)) nb.elements.forEach((el) => names.push((el.propertyName ?? el.name).text));
        if (nb && ts.isNamespaceImport(nb)) names.push("cookies");
      } else if (n.exportClause && ts.isNamedExports(n.exportClause)) {
        n.exportClause.elements.forEach((el) => names.push((el.propertyName ?? el.name).text));
      } else names.push("cookies");
      check(n.moduleSpecifier.text, names);
    }
    if (ts.isCallExpression(n) && n.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const a = n.arguments[0];
      if (a && ts.isStringLiteral(a)) check(a.text, ["cookies"]);
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return hits;
}

describe("TBD-624 F1: no cookie-identity server read", () => {
  it("nothing under app/, lib/, components/ imports cookies() or lib/auth-server", () => {
    const bad: string[] = [];
    for (const dir of ["app", "lib", "components"]) {
      for (const f of walk(path.join(ROOT, dir))) {
        for (const h of offences(f)) bad.push(`${path.relative(ROOT, f)}: ${h}`);
      }
    }
    expect(bad).toEqual([]);
  });
});
