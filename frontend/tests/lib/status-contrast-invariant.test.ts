/**
 * Status ink / tint contrast invariants (TBD-483).
 *
 * `app/globals.css` defines four status colours (danger, warning, info,
 * success) and, for two of them (danger, warning), a solid "-text" ink meant
 * to sit on the solid fill. Each status also has a translucent "-dim" tint
 * meant to sit *behind* the status ink as a badge/banner background. Neither
 * relationship was ever checked for WCAG 2.2 AA (4.5:1) — two architects
 * independently measured several combinations failing, mostly in the light
 * theme.
 *
 * This fence has two halves:
 *
 *  1. Contrast: it derives the (ink, tint) and (fill, "-text" ink) pairs from
 *     the actual primitives in `lib/styles.ts` (not a hand-typed list, so a
 *     new primitive is covered automatically), composites each translucent
 *     tint over every plain host colour the app renders behind it, and
 *     asserts >= 4.5:1 in both themes. It also checks each bare status ink
 *     directly against every host (an ink can appear on a plain surface, not
 *     just its own tint), and checks each solid "-text" ink against its fill
 *     and hover-fill.
 *
 *  2. No bypass: a second test bans any NEW static `bg-<status>/<opacity>`
 *     ad-hoc tint (opacity <= 30%, including the Tailwind important modifier
 *     in either position — `!bg-danger/10` or `bg-danger/10!` — and an
 *     arbitrary-value alpha like `bg-danger/[0.08]`) in `app/`, `components/`
 *     or `lib/`. This is exactly the pattern TBD-483 migrated onto the
 *     checked primitives; a call site that reintroduced one would sit
 *     outside everything (1) measures.
 *
 *  3. State tints: a *stateful* tint (`hover:`, `group-hover:`, `focus:`,
 *     `focus-visible:`, `active:` prefixing `bg-<status>/<alpha>`) is exempt
 *     from (2) — it is a legitimate transient-state fill, not the ad-hoc
 *     pattern being banned — but it is not exempt from being CORRECT: every
 *     distinct (status, alpha) state tint found anywhere in the scan, paired
 *     with a same-status `text-<status>` ink on the same element, is
 *     measured exactly like (1)'s tints: >= 4.5:1 composited over every host,
 *     in both themes.
 *
 * Hosts checked: `surface`, `surface-raised`, `bg`. `surface-overlay` is
 * deliberately excluded — no status ink or status tint is ever rendered on
 * it (modals/menus render on surface-raised or surface); there is nothing to
 * check there.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import postcss from "postcss";
import ts from "typescript";
import { describe, it, expect } from "vitest";
import { hexToRgb, parseColor, compositeOver, contrast, block, type Vec3 } from "./color-test-utils";
import * as styles from "../../lib/styles";

const CONTRAST_MIN = 4.5;
const STATUSES = ["danger", "warning", "success", "info"] as const;
type Status = (typeof STATUSES)[number];

const FRONTEND_ROOT = path.resolve(__dirname, "..", "..");
const GLOBALS = path.join(FRONTEND_ROOT, "app", "globals.css");
const css = readFileSync(GLOBALS, "utf8");
const sheet = postcss.parse(css, { from: GLOBALS });

const BLOCKS = { dark: ":root", light: '[data-theme="light"]' } as const;
type Theme = keyof typeof BLOCKS;
const THEMES: Theme[] = ["light", "dark"];
const HOSTS = ["surface", "surface-raised", "bg"] as const;
type Host = (typeof HOSTS)[number];

function themeTokens(theme: Theme): Map<string, string> {
  return block(sheet, BLOCKS[theme]);
}

function tokenValue(theme: Theme, name: string): string {
  const key = `--theme-${name}`;
  const v = themeTokens(theme).get(key);
  if (v === undefined) throw new Error(`${key} missing from ${BLOCKS[theme]}`);
  return v;
}

// ─── deriving pairs from lib/styles.ts ────────────────────────────────────

const STATUS_RE = STATUSES.join("|");
/** `text-<status>`, but not `text-<status>-text` (a bare ink usage). */
const INK_RE = new RegExp(`\\btext-(${STATUS_RE})\\b(?!-text)`, "g");
/** `bg-<status>-dim` (a tint fill). */
const TINT_RE = new RegExp(`\\bbg-(${STATUS_RE})-dim\\b`, "g");
/** `bg-<status>`, excluding the `-dim`/`-hover` variants (a solid fill). */
const FILL_RE = new RegExp(`\\bbg-(${STATUS_RE})\\b(?!-dim|-hover)`, "g");
/** `bg-<status>-hover` (a solid hover fill). */
const HOVER_FILL_RE = new RegExp(`\\bbg-(${STATUS_RE})-hover\\b`, "g");
/** `text-<status>-text` (the solid ink meant for a fill). */
const FILL_TEXT_RE = new RegExp(`\\btext-(${STATUS_RE})-text\\b`, "g");

function matchesOf(re: RegExp, text: string): Status[] {
  return [...text.matchAll(re)].map((m) => m[1] as Status);
}

interface InkOnTint {
  source: string;
  ink: Status;
  tint: Status;
}
interface TextOnFill {
  source: string;
  text: Status;
  fill: Status;
  hover: boolean;
}

function deriveInkOnTintPairs(): InkOnTint[] {
  const out: InkOnTint[] = [];
  for (const [name, value] of Object.entries(styles)) {
    if (typeof value !== "string") continue;
    const tints = matchesOf(TINT_RE, value);
    const inks = matchesOf(INK_RE, value);
    if (tints.length === 0 || inks.length === 0) continue;
    for (const tint of tints) {
      for (const ink of inks) out.push({ source: name, ink, tint });
    }
  }
  return out;
}

function deriveTextOnFillPairs(): TextOnFill[] {
  const out: TextOnFill[] = [];
  for (const [name, value] of Object.entries(styles)) {
    if (typeof value !== "string") continue;
    const texts = matchesOf(FILL_TEXT_RE, value);
    if (texts.length === 0) continue;
    for (const fill of matchesOf(FILL_RE, value)) {
      for (const t of texts) out.push({ source: name, text: t, fill, hover: false });
    }
    for (const fill of matchesOf(HOVER_FILL_RE, value)) {
      for (const t of texts) out.push({ source: name, text: t, fill, hover: true });
    }
  }
  return out;
}

// ─── colour lookups ────────────────────────────────────────────────────────

function hostRgb(theme: Theme, host: Host): Vec3 {
  return hexToRgb(tokenValue(theme, host));
}

function inkRgb(theme: Theme, status: Status): Vec3 {
  return hexToRgb(tokenValue(theme, status));
}

function tintOnHost(theme: Theme, status: Status, host: Host): Vec3 {
  const tint = parseColor(tokenValue(theme, `${status}-dim`));
  return compositeOver(tint, hostRgb(theme, host));
}

/** A `bg-<status>/<alphaPct>` state tint (the status ink at `alphaPct`%
 * opacity, e.g. Tailwind's `bg-danger/10`) composited over a host. Distinct
 * from `tintOnHost`, which reads the theme's own `-dim` token: an ad-hoc
 * alpha utility does not go through that token at all. */
function stateTintOnHost(theme: Theme, status: Status, alphaPct: number, host: Host): Vec3 {
  return compositeOver({ rgb: inkRgb(theme, status), alpha: alphaPct / 100 }, hostRgb(theme, host));
}

function fillRgb(theme: Theme, status: Status, hover: boolean): Vec3 {
  return hexToRgb(tokenValue(theme, hover ? `${status}-hover` : status));
}

// ─── reference-vector guard ─────────────────────────────────────────────

describe("contrast maths reference vectors", () => {
  it("matches known WCAG contrast values", () => {
    expect(contrast(hexToRgb("#000000"), hexToRgb("#ffffff"))).toBeCloseTo(21.0, 1);
    expect(contrast(hexToRgb("#777777"), hexToRgb("#ffffff"))).toBeCloseTo(4.48, 2);
  });

  it("parseColor and compositeOver match a known composite", () => {
    // rgba(255, 0, 0, 0.5) over #ffffff -> #ff8080 ([255, 128, 128]).
    const red50 = parseColor("rgba(255, 0, 0, 0.5)");
    expect(red50.alpha).toBeCloseTo(0.5, 5);
    const composited = compositeOver(red50, hexToRgb("#ffffff"));
    expect(composited.map((c) => Math.round(c * 255))).toEqual([255, 128, 128]);

    // #rrggbb has alpha 1 and round-trips exactly.
    const solid = parseColor("#336699");
    expect(solid.alpha).toBe(1);
    expect(solid.rgb.map((c) => Math.round(c * 255))).toEqual([0x33, 0x66, 0x99]);
  });
});

// ─── anti-vacuity ────────────────────────────────────────────────────────

describe("derived pair population", () => {
  it("finds at least one ink-on-tint pair, including badgeError and error", () => {
    const pairs = deriveInkOnTintPairs();
    expect(pairs.length).toBeGreaterThan(0);
    expect(pairs.some((p) => p.source === "badgeError")).toBe(true);
    expect(pairs.some((p) => p.source === "error")).toBe(true);
  });

  it("every status in STATUSES appears as a derived ink-on-tint pair", () => {
    const pairs = deriveInkOnTintPairs();
    for (const status of STATUSES) {
      expect(pairs.some((p) => p.tint === status), `${status} missing from derived tint pairs`).toBe(true);
    }
  });
});

// ─── the contrast assertions ─────────────────────────────────────────────

describe.each(THEMES)("%s theme", (theme) => {
  it("every ink-on-tint pair from lib/styles.ts is >= 4.5:1 composited over every host", () => {
    const pairs = deriveInkOnTintPairs();
    const failures: string[] = [];
    for (const { source, ink, tint } of pairs) {
      for (const host of HOSTS) {
        const cr = contrast(inkRgb(theme, ink), tintOnHost(theme, tint, host));
        if (cr < CONTRAST_MIN) {
          failures.push(`${source}: text-${ink} on bg-${tint}-dim over ${host} = ${cr.toFixed(2)}:1`);
        }
      }
    }
    expect(failures).toEqual([]);
  });

  it("every status ink is >= 4.5:1 on every plain host", () => {
    const failures: string[] = [];
    for (const status of STATUSES) {
      for (const host of HOSTS) {
        const cr = contrast(inkRgb(theme, status), hostRgb(theme, host));
        if (cr < CONTRAST_MIN) {
          failures.push(`text-${status} on ${host} = ${cr.toFixed(2)}:1`);
        }
      }
    }
    expect(failures).toEqual([]);
  });

  it("danger/warning '-text' is >= 4.5:1 on its fill and hover fill", () => {
    const pairs = deriveTextOnFillPairs();
    expect(pairs.length).toBeGreaterThan(0);
    const failures: string[] = [];
    // -text tokens are their own colour, not derived from `status`, so look
    // them up directly rather than through inkRgb (which reads `--theme-<status>`).
    for (const { source, text, fill, hover } of pairs) {
      const textRgb = hexToRgb(tokenValue(theme, `${text}-text`));
      const cr = contrast(textRgb, fillRgb(theme, fill, hover));
      if (cr < CONTRAST_MIN) {
        failures.push(
          `${source}: text-${text}-text on bg-${fill}${hover ? "-hover" : ""} = ${cr.toFixed(2)}:1`,
        );
      }
    }
    expect(failures).toEqual([]);
  }, 20000);
});


// ─── no ad-hoc bypass, and measuring the state tints that ARE allowed ────

const SCAN_ROOTS = ["app", "components", "lib"].map((d) => path.join(FRONTEND_ROOT, d));

/** Variants that make a tint a transient STATE fill rather than a static
 * ad-hoc one. `focus-visible` must precede `focus` in the alternation so it
 * is tried first — the engine backtracks past a bare `focus` match that
 * fails to find the immediately-following `:`, but only if there is a
 * longer alternative left to try. */
const STATE_PREFIXES = ["hover", "group-hover", "focus-visible", "focus", "active"] as const;
const STATE_PREFIX_RE = new RegExp(`^(${STATE_PREFIXES.join("|")}):`);

/** `bg-<status>/<alpha>`, alpha as a percent integer (`/10`) or an
 * arbitrary-value fraction (`/[0.08]`, 0..1). Matched against a token with
 * its variant prefix already stripped. */
const TINT_TOKEN_RE = new RegExp(`^bg-(${STATUS_RE})/(?:(\\d+)|\\[(0?\\.\\d+|1(?:\\.0+)?)\\])$`);

interface TintToken {
  status: Status;
  alphaPct: number;
  state: string | null;
}

/** Parses one whitespace-split class token into a status tint, or null if it
 * isn't one. Handles Tailwind's important modifier in BOTH positions —
 * v3's `!bg-danger/10` and v4's `bg-danger/10!` (and either position after a
 * state prefix) — by stripping every `!`: the character has no other use in
 * these tokens. */
function parseTintToken(rawToken: string): TintToken | null {
  const core = rawToken.replace(/!/g, "");
  const stateMatch = STATE_PREFIX_RE.exec(core);
  const rest = stateMatch ? core.slice(stateMatch[0].length) : core;
  const m = TINT_TOKEN_RE.exec(rest);
  if (!m) return null;
  const alphaPct = m[2] !== undefined ? Number(m[2]) : Number(m[3]) * 100;
  return { status: m[1] as Status, alphaPct, state: stateMatch ? stateMatch[1] : null };
}

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full));
    else if (full.endsWith(".tsx") || full.endsWith(".ts")) out.push(full);
  }
  return out;
}

function classStrings(file: string): { text: string; line: number }[] {
  const src = readFileSync(file, "utf8");
  if (!/bg-(danger|warning|success|info)\//.test(src)) return [];
  const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const out: { text: string; line: number }[] = [];
  const visit = (n: ts.Node): void => {
    if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) {
      out.push({ text: n.text, line: sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1 });
    } else if (ts.isTemplateExpression(n)) {
      out.push({ text: n.head.text, line: sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1 });
      for (const span of n.templateSpans) {
        out.push({
          text: span.literal.text,
          line: sf.getLineAndCharacterOfPosition(span.literal.getStart(sf)).line + 1,
        });
      }
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return out;
}

/** Every scanned file, computed once: shared by the population guard and by
 * every scan below so a >50-file walk isn't repeated per test. */
const SCANNED_FILES = SCAN_ROOTS.flatMap(walk);

describe("no ad-hoc status tint bypasses the checked primitives", () => {
  it("the scan actually walked the tree (population guard)", () => {
    // An empty or broken walk would make the ban below pass vacuously.
    expect(SCANNED_FILES.length).toBeGreaterThan(50);
    expect(
      SCANNED_FILES.some((f) => path.relative(FRONTEND_ROOT, f) === "lib/styles.ts"),
    ).toBe(true);
  });

  it("app/, components/ and lib/ hold no static bg-<status>/<alpha> <= 30%, outside a state prefix", () => {
    const failures: string[] = [];
    // ponytail: walking + TS-parsing every app/components/lib file is slow
    // under CI/container CPU contention (measured >5s cold); this is the
    // full-tree scan focus-baseline.test.ts also does, at its own timeout.
    for (const file of SCANNED_FILES) {
      for (const { text, line } of classStrings(file)) {
        for (const token of text.split(/\s+/).filter(Boolean)) {
          const parsed = parseTintToken(token);
          if (!parsed) continue;
          if (parsed.state) continue; // a state tint: exempt here, measured below
          if (parsed.alphaPct <= 30) {
            failures.push(`${path.relative(FRONTEND_ROOT, file)}:${line} ${token}`);
          }
        }
      }
    }
    expect(failures).toEqual([]);
  }, 20000);
});

// ─── state tints (hover:/focus:/... bg-<status>/N + text-<status>) ──────

interface StateTintPair {
  status: Status;
  alphaPct: number;
  sources: string[];
}

/** Every distinct (status, alphaPct) state tint in the scan that is paired,
 * on the same class string, with a plain `text-<same status>` ink — i.e.
 * the ink that will actually sit on that tint. An unpaired state tint (a
 * fill with no status text on it) has nothing to measure. */
function deriveStateTintPairs(): StateTintPair[] {
  const byKey = new Map<string, StateTintPair>();
  for (const file of SCANNED_FILES) {
    for (const { text, line } of classStrings(file)) {
      const inks = new Set(matchesOf(INK_RE, text));
      for (const token of text.split(/\s+/).filter(Boolean)) {
        const parsed = parseTintToken(token);
        if (!parsed || !parsed.state || !inks.has(parsed.status)) continue;
        const key = `${parsed.status}:${parsed.alphaPct}`;
        const source = `${path.relative(FRONTEND_ROOT, file)}:${line}`;
        const existing = byKey.get(key);
        if (existing) existing.sources.push(source);
        else byKey.set(key, { status: parsed.status, alphaPct: parsed.alphaPct, sources: [source] });
      }
    }
  }
  return [...byKey.values()];
}

describe("state tint population", () => {
  it("finds at least one (status, alpha) state tint, including danger at 10%", () => {
    const pairs = deriveStateTintPairs();
    expect(pairs.length).toBeGreaterThan(0);
    expect(pairs.some((p) => p.status === "danger" && p.alphaPct === 10)).toBe(true);
  });
});

describe.each(THEMES)("%s theme: state tints", (theme) => {
  it("every (status, alpha) state tint is >= 4.5:1 composited over every host", () => {
    const pairs = deriveStateTintPairs();
    const failures: string[] = [];
    for (const { status, alphaPct, sources } of pairs) {
      for (const host of HOSTS) {
        const cr = contrast(inkRgb(theme, status), stateTintOnHost(theme, status, alphaPct, host));
        if (cr < CONTRAST_MIN) {
          failures.push(
            `text-${status} on bg-${status}/${alphaPct} (state) over ${host} = ${cr.toFixed(2)}:1 ` +
              `(${sources.length} site(s), e.g. ${sources[0]})`,
          );
        }
      }
    }
    expect(failures).toEqual([]);
  }, 20000);
});

