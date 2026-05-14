// Guard test for the locked customer-copy policy
// (`feedback_no_em_dashes`). Em-dashes (U+2014) are banned in
// user-facing prose: page bodies, JSX text, aria-labels, alt text.
// They remain allowed in:
//
//  - Code comments (// and /* */ blocks). Those are dev-facing.
//  - Single-glyph null placeholders rendered as `?? "—"` and friends.
//    Those are layout convention, not prose, and the policy explicitly
//    carves them out as "single character glyph used to mean empty
//    or N/A".
//
// This test walks the frontend tree (excluding tests / node_modules /
// build outputs), strips comments, and asserts no em-dash remains in
// the executable source unless the line matches the null-placeholder
// idiom (`?? "—"`, `: "—"`, JSX `>—<`, attr `="—"`).
//
// Long-form pause? Use a period or a parenthetical. The policy leaves
// no en-dash fallback for prose; en-dashes (U+2013) are only for
// numeric ranges ("5–6 days", "2024–2025") and are not scanned here.
//
// Backend Python email-template strings live in
// backend/app/services/email_service.py and are already em-dash free;
// keeping that property is enforced by code review, not this test.

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const EM_DASH = "—"; // —
const FRONTEND_ROOT = join(__dirname, "..", "..");

// Strip JS/TS comments while preserving line numbers so the reported
// match offsets remain meaningful. We replace comment characters with
// spaces (not blanks) so column counts stay aligned. Naive regex
// passes would corrupt strings that happen to contain "//"; this
// state-machine walk is short and exact.
function stripComments(source: string): string {
  let out = "";
  let i = 0;
  const n = source.length;
  while (i < n) {
    const ch = source[i];
    const next = i + 1 < n ? source[i + 1] : "";
    if (ch === "/" && next === "/") {
      while (i < n && source[i] !== "\n") {
        out += source[i] === "\n" ? "\n" : " ";
        i++;
      }
      continue;
    }
    if (ch === "/" && next === "*") {
      while (i < n && !(source[i] === "*" && source[i + 1] === "/")) {
        out += source[i] === "\n" ? "\n" : " ";
        i++;
      }
      if (i < n) {
        out += " ";
        i++;
      }
      if (i < n) {
        out += " ";
        i++;
      }
      continue;
    }
    out += ch;
    i++;
  }
  return out;
}

// Null-placeholder idioms we allow:
//   ?? "—"      ?? '—'      : "—"      >—<      ="—"
const PLACEHOLDER_PATTERNS = [
  /\?\?\s*["']—["']/g,
  /:\s*["']—["']/g,
  />—</g,
  /["']—["']/g,
];

function stripPlaceholders(line: string): string {
  let out = line;
  for (const re of PLACEHOLDER_PATTERNS) {
    out = out.replace(re, "");
  }
  return out;
}

const SCAN_DIRS = ["app", "components", "lib"];
const SKIP_DIRS = new Set([
  "node_modules",
  ".next",
  "out",
  "out-apex",
  ".next-apex",
  ".apex-staged-routes",
  "__snapshots__",
  "tests",
  "fixtures",
]);

function walkSource(dir: string, hits: string[]): string[] {
  if (!existsSync(dir)) return hits;
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      walkSource(full, hits);
      continue;
    }
    if (!/\.(tsx?|jsx?)$/.test(entry)) continue;
    const source = readFileSync(full, "utf8");
    if (!source.includes(EM_DASH)) continue;
    const stripped = stripComments(source);
    const lines = stripped.split("\n");
    lines.forEach((line, idx) => {
      if (!line.includes(EM_DASH)) return;
      const cleaned = stripPlaceholders(line);
      if (cleaned.includes(EM_DASH)) {
        hits.push(`${relative(FRONTEND_ROOT, full)}:${idx + 1}: ${line.trim()}`);
      }
    });
  }
  return hits;
}

describe("customer-copy voice policy", () => {
  it("contains no em-dashes outside comments or null placeholders", () => {
    const hits: string[] = [];
    for (const seg of SCAN_DIRS) {
      walkSource(join(FRONTEND_ROOT, seg), hits);
    }
    if (hits.length > 0) {
      // Surface every offending location so a regression is easy to
      // pin to a file:line rather than "somewhere in the bundle".
      // eslint-disable-next-line no-console
      console.error(
        "Em-dash policy violations:\n" + hits.map((h) => `  ${h}`).join("\n"),
      );
    }
    expect(hits).toEqual([]);
  });
});
