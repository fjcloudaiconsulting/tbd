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
 *  2. No bypass: a second test bans any NEW `bg-<status>/<opacity>` ad-hoc
 *     tint (opacity 30 or less) in `app/` or `components/` (outside a `hover:` fill), which is
 *     exactly the pattern TBD-483 migrated onto the checked primitives. A
 *     call site that reintroduced one would sit outside everything (1)
 *     measures.
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

function fillRgb(theme: Theme, status: Status, hover: boolean): Vec3 {
  return hexToRgb(tokenValue(theme, hover ? `${status}-hover` : status));
}

// ─── reference-vector guard ─────────────────────────────────────────────

describe("contrast maths reference vectors", () => {
  it("matches known WCAG contrast values", () => {
    expect(contrast(hexToRgb("#000000"), hexToRgb("#ffffff"))).toBeCloseTo(21.0, 1);
    expect(contrast(hexToRgb("#777777"), hexToRgb("#ffffff"))).toBeCloseTo(4.48, 2);
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


// ─── no ad-hoc bypass ──────────────────────────────────────────────────

const SCAN_ROOTS = ["app", "components"].map((d) => path.join(FRONTEND_ROOT, d));
// A TINT is a low-alpha wash that text sits on (<= 30%). A high-alpha
// status fill (e.g. the landing hero's decorative `/80` bars) carries no
// text and is not what this fence measures, so it is not banned.
const AD_HOC_TINT_RE = new RegExp(`(^|:)bg-(${STATUS_RE})/([0-9]|[12][0-9]|30)$`);

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

describe("no ad-hoc status tint bypasses the checked primitives", () => {
  it("app/ and components/ hold no bare bg-<status>/<n>, outside hover:", () => {
    const failures: string[] = [];
    for (const root of SCAN_ROOTS) {
      // ponytail: walking + TS-parsing every app/components file is slow
      // under CI/container CPU contention (measured >5s cold); this is the
      // full-tree scan focus-baseline.test.ts also does, at its own timeout.
      for (const file of walk(root)) {
        for (const { text, line } of classStrings(file)) {
          for (const token of text.split(/\s+/).filter(Boolean)) {
            if (/^hover:/.test(token)) continue;
            if (AD_HOC_TINT_RE.test(token)) {
              failures.push(`${path.relative(FRONTEND_ROOT, file)}:${line} ${token}`);
            }
          }
        }
      }
    }
    expect(failures).toEqual([]);
  }, 20000);
});

