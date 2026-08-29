import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-459 — a test that stubs ``requestAnimationFrame`` must also fake timers.
 *
 * ## The incident
 *
 * On 2026-08-29 both post-merge ``Test`` runs on ``main`` failed while
 * reporting **365 files / 2992 tests passed, zero test failures**. A re-run of
 * the identical commit went green. The failure was an UNHANDLED error:
 *
 *     ReferenceError: cancelAnimationFrame is not defined
 *       ❯ Timeout.callback @reduxjs/toolkit/src/autoBatchEnhancer.ts:23
 *     This error was caught after test environment was torn down.
 *
 * ## The mechanism
 *
 * recharts 3.x drives its internal store with Redux Toolkit. RTK's
 * ``autoBatchEnhancer`` schedules through ``createRafWithFallbackTimer``,
 * which arms BOTH ``raf(callback)`` and ``setTimeout(callback, timeout)``;
 * whichever fires first calls ``cancelAnimationFrame(rafId)`` — a BARE global
 * lookup, resolved when it runs — and clears the other.
 *
 * ``tests/components/charts/reduced-motion.test.tsx`` stubs
 * ``requestAnimationFrame`` with a function that returns a handle and NEVER
 * invokes its callback. That is deliberate and load-bearing: the fence needs
 * first-paint geometry at ``t === 0``. But it means the rAF arm can never
 * fire, ``clearTimeout`` never runs, and the fallback timer is left armed with
 * nothing to cancel it. Measured: **2** such calls escape the test body under
 * real timers, **0** under fake ones — and 2 is exactly the number of
 * unhandled errors CI reported.
 *
 * On a fast machine that timer lands during the test and nothing happens. On a
 * loaded runner it lands after teardown and takes the whole suite down.
 *
 * ## Why a convention gate rather than only the fix
 *
 * The fix is three lines in one file, and the next person to stub rAF — for a
 * canvas test, a scroll test, an animation test — reintroduces it with no
 * warning. The failure mode is a FLAKE, so it will not be caught by the PR
 * that causes it; it detonates later, on someone else's merge, with a green
 * local run. That is worth a gate.
 *
 * ⚠ This is a PARSE, not a grep, for the reason its sibling
 * ``chart-animation-gate.test.ts`` documents at length: a regex over source
 * is satisfied by a comment and defeated by an idiomatic ``/*`` inside a line
 * comment. Comments and string literals never reach an AST visitor.
 */

const TESTS_ROOT = path.resolve(__dirname, "..");
const SKIP_DIRS = new Set(["node_modules", "fixtures", "__snapshots__"]);
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

/** ``vi.<name>(...)`` / ``vitest.<name>(...)``, read off the AST. */
function callsViMethod(node: ts.Node, name: string): boolean {
  if (!ts.isCallExpression(node)) return false;
  const callee = node.expression;
  return (
    ts.isPropertyAccessExpression(callee) &&
    ts.isIdentifier(callee.name) &&
    callee.name.text === name &&
    ts.isIdentifier(callee.expression) &&
    (callee.expression.text === "vi" || callee.expression.text === "vitest")
  );
}

type Scan = { stubsRaf: boolean; fakesTimers: boolean };

function scan(file: string): Scan {
  const source = readFileSync(file, "utf8");
  // Cheap pre-filter on RAW source. Safe in this direction only: a file that
  // never mentions the identifier cannot stub it. A file that mentions it in
  // prose is parsed, and then found clean.
  if (!source.includes("requestAnimationFrame")) {
    return { stubsRaf: false, fakesTimers: false };
  }

  const sf = ts.createSourceFile(
    file,
    source,
    ts.ScriptTarget.Latest,
    true,
    file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );

  let stubsRaf = false;
  let fakesTimers = false;

  const visit = (node: ts.Node): void => {
    if (callsViMethod(node, "stubGlobal")) {
      const [first] = (node as ts.CallExpression).arguments;
      if (
        first !== undefined &&
        ts.isStringLiteralLike(first) &&
        first.text === "requestAnimationFrame"
      ) {
        stubsRaf = true;
      }
    }
    if (callsViMethod(node, "useFakeTimers")) fakesTimers = true;
    ts.forEachChild(node, visit);
  };
  visit(sf);

  return { stubsRaf, fakesTimers };
}

describe("TBD-459: stubbing requestAnimationFrame requires fake timers", () => {
  const files = walk(TESTS_ROOT);
  const stubbing = files.filter((f) => scan(f).stubsRaf);

  it("finds test files to police — the scan is not vacuous", () => {
    // ⚠ Anti-vacuity floor. If the scan silently matches nothing (a moved
    // tests root, a renamed helper, an AST shape this visitor stopped
    // recognising), every assertion below passes over an empty set and the
    // gate is decoration. Same posture as
    // `scripts/ci/assert-app-spec-secrets-synced.sh`'s `len(committed) < 5`.
    expect(files.length).toBeGreaterThan(100);
    expect(stubbing.length).toBeGreaterThan(0);
  });

  it("every file stubbing rAF also fakes timers", () => {
    const offenders = stubbing
      .filter((f) => !scan(f).fakesTimers)
      .map((f) => path.relative(TESTS_ROOT, f));

    expect(offenders, offenders.length === 0 ? "" : [
      "",
      "These test files stub requestAnimationFrame without vi.useFakeTimers():",
      ...offenders.map((f) => `  tests/${f}`),
      "",
      "A no-op rAF stub strands Redux Toolkit's autoBatch fallback setTimeout",
      "(recharts 3.x uses RTK internally). When it lands after teardown it throws",
      "  ReferenceError: cancelAnimationFrame is not defined",
      "as an UNHANDLED error, failing the whole suite with zero failing tests --",
      "on someone else's merge, not yours. Fake the timers so it never reaches",
      "the real event loop. See TBD-459.",
    ].join("\n")).toEqual([]);
  });
});
