import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-624 F1. No server render may act as the refresh cookie's identity: the
 * cookie is shared by every tab, so a server read keyed on it returns whichever
 * account signed in last. PARSES imports (a grep is satisfied by a comment).
 * Any form of next/headers (named, aliased, default, namespace, .js, dynamic,
 * require) is banned except lib/nonce.ts, which reads only the CSP nonce header.
 * Kills: re-adding a cookie read via next/headers, or `@/lib/auth-server`.
 * Ceiling: a non-literal specifier; `headers()` read in nonce.ts itself.
 */
const ROOT = path.resolve(__dirname, "..", "..");
const NONCE = "lib/nonce.ts";
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

const norm = (spec: string) => spec.replace(/\.(m?js)$/, "");

function offences(file: string, rel: string): string[] {
  const text = readFileSync(file, "utf8");
  // Cheap prefilter only (it can over-select, never decide): parsing ~600 files is slow under load.
  if (!text.includes("next/headers") && !text.includes("auth-server")) return [];
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const hits: string[] = [];
  const check = (raw: string) => {
    const spec = norm(raw);
    if (/(^|\/)auth-server$/.test(spec)) hits.push(`imports ${raw}`);
    if (spec === "next/headers" && rel !== NONCE) hits.push(`imports ${raw}`);
  };
  const visit = (n: ts.Node): void => {
    if ((ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) && n.moduleSpecifier && ts.isStringLiteral(n.moduleSpecifier)) {
      check(n.moduleSpecifier.text);
    }
    if (ts.isImportEqualsDeclaration(n) && ts.isExternalModuleReference(n.moduleReference) && ts.isStringLiteral(n.moduleReference.expression)) {
      check(n.moduleReference.expression.text);
    }
    if (ts.isCallExpression(n) && (n.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(n.expression) && n.expression.text === "require"))) {
      const a = n.arguments[0];
      if (a && (ts.isStringLiteral(a) || ts.isNoSubstitutionTemplateLiteral(a))) check(a.text);
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return hits;
}

describe("TBD-624 F1: no cookie-identity server read", () => {
  it("only lib/nonce.ts imports next/headers; nothing imports lib/auth-server", () => {
    const rootFiles = readdirSync(ROOT)
      .filter((e) => /\.(tsx?|jsx?|mjs|cjs)$/.test(e))
      .map((e) => path.join(ROOT, e));
    const files = [...["app", "lib", "components"].flatMap((d) => walk(path.join(ROOT, d))), ...rootFiles];
    expect(rootFiles.map((f) => path.basename(f))).toContain("proxy.ts");
    const bad: string[] = [];
    for (const f of files) {
      const rel = path.relative(ROOT, f).split(path.sep).join("/");
      for (const h of offences(f, rel)) bad.push(`${rel}: ${h}`);
    }
    expect(bad).toEqual([]);
  });
});
