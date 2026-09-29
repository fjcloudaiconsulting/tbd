/**
 * TBD-482: docs/design/DESIGN.md's colour values must equal the runtime.
 *
 * `frontend/app/globals.css` is authoritative for VALUES; DESIGN.md is
 * authoritative for ROLES and RULES. The doc drifted anyway (`text-muted` kept
 * a value the runtime had abandoned for failing WCAG 1.4.3), and two readers of
 * the stale doc produced the same wrong contrast figure, which read as
 * corroboration. This fails rather than regenerates: a red here means read the
 * diff and decide which side is right.
 *
 * Three assertions, because the doc holds colour in two places:
 *   1. every frontmatter `colors:` entry equals the dark tier (`:root`)
 *      `--theme-*` token of the same name (`chart-N` is `--theme-cat-N`);
 *   2. every `:root` colour token has a frontmatter entry (the reverse);
 *   3. the prose quotes no colour literal at all, so a light-theme value (or
 *      any other) cannot drift there. Prose names tokens; values live in
 *      globals.css.
 *
 * ⚠ The doc lives outside frontend/, so docker-compose mounts `docs/design`
 * into the container, and CI's change detection classifies a DESIGN.md edit
 * as a frontend change. Without the second, this fence is skipped on exactly
 * the docs-only PR that drifts the doc.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import postcss from "postcss";
import { describe, expect, it } from "vitest";

const FRONTEND_ROOT = path.resolve(__dirname, "..", "..");
const GLOBALS = path.join(FRONTEND_ROOT, "app", "globals.css");
const DESIGN_MD = path.resolve(FRONTEND_ROOT, "..", "docs", "design", "DESIGN.md");

// Any quoted colour value: 6/8-digit hex, 3/4-digit hex that contains a
// letter, or a CSS colour function (any case). ⚠ All-digit short hex (`#000`,
// `#999`) is deliberately NOT matched, so PR references like `#378` stay
// legal; black and greys are the likeliest short literals, so prefer the
// token name in prose. A hex-letter word before `-` or `)` (`#add-x`) would
// match; nothing in the doc does that today.
const COLOUR_LITERAL =
  /#[0-9a-f]{6}(?:[0-9a-f]{2})?\b|#(?=[0-9a-f]{0,3}[a-f])[0-9a-f]{3,4}\b|\b(?:rgba?|hsla?|hwb|oklab|oklch|lab|lch|color)\(/gi;

/** `:root` tokens that are not colours, so they have no frontmatter entry. */
const NON_COLOUR_TOKENS = new Set(["card-shadow"]);

function splitDoc(): { frontmatter: string; body: string } {
  const text = readFileSync(DESIGN_MD, "utf8");
  const m = /^---\n([\s\S]*?)\n---\n([\s\S]*)$/.exec(text);
  if (!m) throw new Error("DESIGN.md has no YAML frontmatter");
  return { frontmatter: m[1], body: m[2] };
}

/** The frontmatter's `colors:` map. A regular `  key: "#hex"` block, so a line
 *  scan is exact here and adds no YAML dependency. */
function docColors(): Record<string, string> {
  const { frontmatter } = splitDoc();
  const lines = frontmatter.split("\n");
  const start = lines.indexOf("colors:");
  if (start < 0) throw new Error("frontmatter has no colors: block");
  const out: Record<string, string> = {};
  for (const line of lines.slice(start + 1)) {
    if (line.trim() === "" || /^\s*#/.test(line)) continue; // blank / YAML comment
    if (!line.startsWith("  ")) break; // next top-level key
    const m = /^ {2}([a-z0-9-]+):\s*"([^"]+)"/.exec(line);
    // Fail loud on any entry this scan cannot read (single quotes, an
    // unquoted `#...` that YAML reads as a comment), so an entry can never
    // drop out of the comparison silently.
    if (!m) throw new Error(`unparsable colors entry in DESIGN.md: ${line.trim()}`);
    out[m[1]] = m[2];
  }
  return out;
}

/** `--theme-*` declarations in the dark tier (`:root`). */
function rootTokens(): Record<string, string> {
  const out: Record<string, string> = {};
  postcss.parse(readFileSync(GLOBALS, "utf8")).walkRules(":root", (rule) => {
    rule.walkDecls(/^--theme-/, (d) => {
      out[d.prop.slice("--theme-".length)] = d.value.trim();
    });
  });
  return out;
}

/** Normalise to lowercase #rrggbb or #rrggbbaa; resolves one level of var(). */
function normalise(value: string, tokens: Record<string, string>): string {
  const ref = /^var\(--theme-([a-z0-9-]+)\)$/.exec(value);
  if (ref) return normalise(tokens[ref[1]] ?? value, tokens);
  const rgba = /^rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)$/.exec(value);
  if (rgba) {
    const [r, g, b] = rgba.slice(1, 4).map(Number);
    const a = Math.round(Number(rgba[4]) * 255);
    return "#" + [r, g, b, a].map((n) => n.toString(16).padStart(2, "0")).join("");
  }
  return value.toLowerCase();
}

function tokenFor(docKey: string): string {
  const chart = /^chart-(\d+)$/.exec(docKey);
  return chart ? `cat-${chart[1]}` : docKey;
}

describe("TBD-482: DESIGN.md colour values match globals.css", () => {
  it("every frontmatter colour equals the dark-tier token of the same name", () => {
    const doc = docColors();
    const tokens = rootTokens();
    const drift = Object.entries(doc)
      .map(([key, docValue]) => {
        const runtime = tokens[tokenFor(key)];
        return runtime === undefined
          ? `${key}: no --theme-${tokenFor(key)} in :root`
          : normalise(docValue, tokens) === normalise(runtime, tokens)
            ? null
            : `${key}: DESIGN.md ${docValue} != globals.css ${runtime}`;
      })
      .filter(Boolean);
    expect(
      drift,
      "DESIGN.md frontmatter disagrees with frontend/app/globals.css :root. " +
        "globals.css is authoritative for values: read the diff, then fix the doc " +
        "(or, if the runtime is wrong, fix the runtime deliberately).",
    ).toEqual([]);
  });

  it("every dark-tier colour token has a frontmatter entry", () => {
    const documented = new Set(Object.keys(docColors()).map(tokenFor));
    const missing = Object.keys(rootTokens()).filter(
      (t) => !documented.has(t) && !NON_COLOUR_TOKENS.has(t),
    );
    expect(missing, "globals.css :root colour tokens missing from DESIGN.md frontmatter").toEqual([]);
  });

  it("the prose quotes no colour value (it names tokens instead)", () => {
    const hexes = splitDoc()
      .body.split("\n")
      .flatMap((line, i) => (line.match(COLOUR_LITERAL) ?? []).map((h) => `body line ${i + 1}: ${h}`));
    expect(
      hexes,
      "DESIGN.md prose must name tokens, not quote values: a quoted colour drifts " +
        "silently when globals.css changes. Values live in globals.css.",
    ).toEqual([]);
  });
});

describe("TBD-482: anti-vacuity", () => {
  it("parses a real, non-trivial colour map from both files", () => {
    // Guards the guard: an empty map on either side would make the
    // comparison vacuously green.
    expect(Object.keys(docColors()).length).toBeGreaterThan(30);
    expect(Object.keys(rootTokens()).length).toBeGreaterThan(30);
    expect(docColors()["text-muted"]).toBeDefined();
  });

  it("normalises the three value shapes the two files use", () => {
    const t = { accent: "#D4A64A" };
    expect(normalise("rgba(212, 166, 74, 0.12)", t)).toBe("#d4a64a1f");
    expect(normalise("var(--theme-accent)", t)).toBe("#d4a64a");
    expect(normalise("#D4A64A1F", t)).toBe("#d4a64a1f");
  });
});
