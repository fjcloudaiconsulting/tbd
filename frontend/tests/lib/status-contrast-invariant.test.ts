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

/** Variants under which a low-alpha status tint is a legitimate transient
 * STATE fill rather than the static ad-hoc pattern being banned. Matched as
 * a WHOLE variant segment (`^...$`), so `focus` cannot accidentally match a
 * `focus-visible` segment — the anchors make alternation order irrelevant.
 * `group-hover` carries an optional Tailwind named-group suffix
 * (`group-hover/row`). Explicitly NOT here: responsive (`md:`), theme
 * (`dark:`), and data/aria variants (`data-[x]:`, `aria-selected:`) — those
 * are static contexts, not transient states, so a tint gated only by one of
 * them is banned like any other static tint. */
const STATE_VARIANT_RE =
  /^(?:(?:(?:group|peer)-)?(?:hover|focus|focus-visible|focus-within|active)(?:\/[\w-]+)?|\[&:(?:hover|focus|focus-visible|focus-within|active)\])$/;

/** `bg-<status>/<alpha-spec>` — the alpha spec is parsed separately (below)
 * so an unparseable form (a CSS var) can be distinguished from "not a tint
 * at all". Matched against the LAST `:`-split segment of a token (the
 * utility), after `!` has been stripped from that segment. */
const TINT_UTILITY_RE = new RegExp(`^bg-(${STATUS_RE})/(.+)$`);

/** Parses a Tailwind opacity spec into a 0-100 percent, or null if the form
 * cannot be measured (e.g. a CSS custom property): `10` (percent int),
 * `[0.08]`/`[.08]` (arbitrary fraction 0..1), `[8%]` (v4 arbitrary percent).
 * Anything else — `(--x)`, `[--x]`, any other CSS-var reference — is
 * deliberately NOT parsed: its rendered alpha is unknown, so it fails
 * closed rather than silently passing as "not a tint". */
function parseAlphaSpec(spec: string): number | null {
  if (/^\d+(?:\.\d+)?$/.test(spec)) return Number(spec);
  let m = /^\[\s*(0?\.\d+|1(?:\.0+)?)\s*\]$/.exec(spec);
  if (m) return Number(m[1]) * 100;
  m = /^\[\s*(\d+(?:\.\d+)?)%\s*\]$/.exec(spec);
  if (m) return Number(m[1]);
  return null;
}

/** Splits a class token on `:`, treating a `:` inside `[...]` as NOT a
 * split point (an arbitrary value like `data-[foo:bar]` must stay one
 * segment). The last segment is the utility; the rest are variants, in
 * order, outermost first. */
function splitVariants(token: string): string[] {
  const segments: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < token.length; i++) {
    const c = token[i];
    if (c === "[") depth++;
    else if (c === "]") depth = Math.max(0, depth - 1);
    else if (c === ":" && depth === 0) {
      segments.push(token.slice(start, i));
      start = i + 1;
    }
  }
  segments.push(token.slice(start));
  return segments;
}

const stripBang = (segment: string): string => segment.replace(/^!/, "").replace(/!$/, "");

type TintParse =
  // A tint this fence can measure: a STATE fill (any alpha) or a static fill
  // above 30%. Measured whenever its own status ink sits on it.
  | { kind: "tint"; status: Status; alphaPct: number; state: boolean }
  | { kind: "banned"; status: Status; alphaPct: number | null; reason: string }
  | null; // not a `bg-<status>/<alpha>` utility at all

/** Parses one whitespace-split class token into a tint verdict. FAILS
 * CLOSED: an alpha spec this parser cannot read is banned outright; a static
 * (no state variant) tint at <= 30% is banned, because static tints must use
 * the checked primitives. Everything else that parses is a measurable tint:
 * a state fill at ANY alpha, or a static fill above 30%. The alpha cut-off
 * decides only what is banned, never what is measured (TBD-483 r3). */
function parseTintToken(rawToken: string): TintParse {
  const segments = splitVariants(rawToken).map(stripBang);
  const utility = segments[segments.length - 1];
  const variants = segments.slice(0, -1);
  const m = TINT_UTILITY_RE.exec(utility);
  if (!m) return null;
  const status = m[1] as Status;
  const alphaSpec = m[2];
  const alphaPct = parseAlphaSpec(alphaSpec);
  if (alphaPct === null) {
    return {
      kind: "banned",
      status,
      alphaPct: null,
      reason: `alpha spec ${JSON.stringify(alphaSpec)} is not a measurable percent or arbitrary fraction/percent (e.g. a CSS var) — cannot be measured, so not allowed`,
    };
  }
  const state = variants.some((v) => STATE_VARIANT_RE.test(v));
  if (state || alphaPct > 30) return { kind: "tint", status, alphaPct, state };
  return {
    kind: "banned",
    status,
    alphaPct,
    reason: `${alphaPct}% tint with no state variant (variants: ${variants.length ? variants.join(":") : "none"})`,
  };
}

describe("parseTintToken", () => {
  const CASES: Array<[string, TintParse]> = [
    // plain, no variant -> banned
    ["bg-danger/10", { kind: "banned", status: "danger", alphaPct: 10, reason: expect.any(String) as unknown as string }],
    ["!bg-danger/10", { kind: "banned", status: "danger", alphaPct: 10, reason: expect.any(String) as unknown as string }],
    ["bg-danger/10!", { kind: "banned", status: "danger", alphaPct: 10, reason: expect.any(String) as unknown as string }],
    ["bg-danger/[0.08]", { kind: "banned", status: "danger", alphaPct: 8, reason: expect.any(String) as unknown as string }],
    ["bg-danger/[8%]", { kind: "banned", status: "danger", alphaPct: 8, reason: expect.any(String) as unknown as string }],
    // unmeasurable alpha spec -> banned regardless of variants
    ["bg-danger/(--x)", { kind: "banned", status: "danger", alphaPct: null, reason: expect.any(String) as unknown as string }],
    // responsive/theme/data/aria variants are NOT state -> banned
    ["md:bg-danger/10", { kind: "banned", status: "danger", alphaPct: 10, reason: expect.any(String) as unknown as string }],
    ["dark:bg-danger/10", { kind: "banned", status: "danger", alphaPct: 10, reason: expect.any(String) as unknown as string }],
    ["data-[x]:bg-danger/10", { kind: "banned", status: "danger", alphaPct: 10, reason: expect.any(String) as unknown as string }],
    ["aria-selected:bg-danger/10", { kind: "banned", status: "danger", alphaPct: 10, reason: expect.any(String) as unknown as string }],
    // a real state variant anywhere in the stack -> measured
    ["hover:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["dark:hover:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["sm:hover:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["hover:md:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["focus-within:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["peer-hover:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["peer-hover/name:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["group-hover/row:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["group-focus:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    ["[&:hover]:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    // a `:` inside brackets is not a split point, so the hover still counts
    ["supports-[a:b]:hover:bg-danger/10", { kind: "tint", status: "danger", alphaPct: 10, state: true }],
    // a state fill above 30% is still measured (r3: the cut-off never hides a pair)
    ["hover:bg-danger/80", { kind: "tint", status: "danger", alphaPct: 80, state: true }],
    // the 30/31 boundary on a static tint: banned at 30, measured at 31
    ["bg-danger/30", { kind: "banned", status: "danger", alphaPct: 30, reason: expect.any(String) as unknown as string }],
    ["bg-danger/31", { kind: "tint", status: "danger", alphaPct: 31, state: false }],
    ["bg-danger/40", { kind: "tint", status: "danger", alphaPct: 40, state: false }],
    ["bg-danger/[.08]", { kind: "banned", status: "danger", alphaPct: 8, reason: expect.any(String) as unknown as string }],
    ["bg-danger/[1]", { kind: "tint", status: "danger", alphaPct: 100, state: false }],
    ["bg-danger/10/", { kind: "banned", status: "danger", alphaPct: null, reason: expect.any(String) as unknown as string }],
    // not a bg-<status>/<alpha> utility at all
    ["hover:text-danger", null],
  ];

  it.each(CASES)("%s", (token, want) => {
    expect(parseTintToken(token)).toEqual(want);
  });
});

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

  it("app/, components/ and lib/ hold no static bg-<status>/<alpha> <= 30%, outside a state variant", () => {
    const failures: string[] = [];
    // ponytail: walking + TS-parsing every app/components/lib file is slow
    // under CI/container CPU contention (measured >5s cold); this is the
    // full-tree scan focus-baseline.test.ts also does, at its own timeout.
    for (const file of SCANNED_FILES) {
      for (const { text, line } of classStrings(file)) {
        for (const token of text.split(/\s+/).filter(Boolean)) {
          const parsed = parseTintToken(token);
          if (!parsed || parsed.kind !== "banned") continue;
          failures.push(`${path.relative(FRONTEND_ROOT, file)}:${line} ${token} (${parsed.reason})`);
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

/** Every distinct (status, alphaPct) measurable tint in the scan (a state
 * fill at any alpha, or a static fill above 30%) that is paired, on the same
 * class string, with a plain `text-<same status>` ink, i.e. the ink that
 * will actually sit on that tint. An unpaired tint has nothing to measure. */
function deriveStateTintPairs(): StateTintPair[] {
  const byKey = new Map<string, StateTintPair>();
  for (const file of SCANNED_FILES) {
    for (const { text, line } of classStrings(file)) {
      const inks = new Set(matchesOf(INK_RE, text));
      for (const token of text.split(/\s+/).filter(Boolean)) {
        const parsed = parseTintToken(token);
        if (!parsed || parsed.kind !== "tint" || !inks.has(parsed.status)) continue;
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

describe("paired tint population", () => {
  it("finds at least one (status, alpha) paired tint, including danger at 10%", () => {
    const pairs = deriveStateTintPairs();
    expect(pairs.length).toBeGreaterThan(0);
    expect(pairs.some((p) => p.status === "danger" && p.alphaPct === 10)).toBe(true);
  });
});

describe.each(THEMES)("%s theme: paired tints (state fills, and static fills above 30%%)", (theme) => {
  it("every (status, alpha) paired tint is >= 4.5:1 composited over every host", () => {
    const pairs = deriveStateTintPairs();
    const failures: string[] = [];
    for (const { status, alphaPct, sources } of pairs) {
      for (const host of HOSTS) {
        const cr = contrast(inkRgb(theme, status), stateTintOnHost(theme, status, alphaPct, host));
        if (cr < CONTRAST_MIN) {
          failures.push(
            `text-${status} on bg-${status}/${alphaPct} over ${host} = ${cr.toFixed(2)}:1 ` +
              `(${sources.length} site(s), e.g. ${sources[0]})`,
          );
        }
      }
    }
    expect(failures).toEqual([]);
  }, 20000);
});

