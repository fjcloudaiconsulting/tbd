import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import path from "node:path";
import postcss from "postcss";
import ts from "typescript";
import { describe, it, expect } from "vitest";

/**
 * TBD-319 — the app's focus baseline.
 *
 * ## What this replaces
 *
 * Before this ticket `app/globals.css` contained exactly ONE focus rule
 * (`input:-webkit-autofill:focus`), so **910 of the app's 973 focusable
 * elements** painted the user-agent default that docs/design/DESIGN.md §5 forbids
 * outright — 427 buttons, 254 inputs, 147 links, 71 selects, 8 textareas.
 * The ticket was filed as "inline prose links have no focus state"; links
 * are 17% of it.
 *
 * ## Why this is a CSS fence and not an element census
 *
 * The obvious fence is "every `<a>` carries a focus class". It is wrong at
 * every level. It would go red on 910 elements; it would need a permanent
 * growing allowlist; and it would *teach contributors to paste classes*,
 * which is the habit that produced two competing idioms and 910 misses. An
 * incomplete census is worse than none, because it certifies coverage it
 * does not have.
 *
 * So the invariant asserted here is the MECHANISM: one global rule, in the
 * right cascade layer, coloured by a token. Everything else follows by
 * inheritance, and the reviewable set becomes the small number of
 * deliberate opt-outs (Part 2).
 *
 * ## ⚠ What no test in this repo can see
 *
 * jsdom implements neither `:focus-visible` nor cascade layers, so no vitest
 * test can prove a brass outline is painted. Anyone who writes one has
 * written a vacuous test. That claim was verified once by hand in a real
 * browser at the visual-approval gate; these fences protect the structure
 * that produces it.
 */

const FRONTEND_ROOT = path.resolve(__dirname, "..", "..");
const GLOBALS = path.join(FRONTEND_ROOT, "app", "globals.css");

const css = readFileSync(GLOBALS, "utf8");
const sheet = postcss.parse(css, { from: GLOBALS });

/** Every rule whose selector is exactly `:focus-visible`. */
function baselineRules(): postcss.Rule[] {
  const found: postcss.Rule[] = [];
  sheet.walkRules((rule) => {
    if (rule.selector.trim() === ":focus-visible") found.push(rule);
  });
  return found;
}

function declOf(rule: postcss.Rule, prop: string): string | undefined {
  let value: string | undefined;
  rule.walkDecls(prop, (d) => {
    value = d.value;
  });
  return value;
}

/** Walk up the postcss tree collecting enclosing at-rules. */
function enclosingAtRules(node: postcss.Node): postcss.AtRule[] {
  const out: postcss.AtRule[] = [];
  let parent = node.parent;
  while (parent) {
    if (parent.type === "atrule") out.push(parent as postcss.AtRule);
    parent = (parent as postcss.Node).parent;
  }
  return out;
}

/** Read a custom-property value out of the first rule matching `selector`. */
function customProp(selector: string, prop: string): string | undefined {
  let value: string | undefined;
  sheet.walkRules((rule) => {
    if (rule.selector.trim() !== selector) return;
    rule.walkDecls(prop, (d) => {
      value = d.value.trim();
    });
  });
  return value;
}

describe("TBD-319 Part 1: the global focus baseline exists, in the right layer", () => {
  it("F1: exactly one `:focus-visible` baseline rule exists", () => {
    // Red against pre-TBD-319 main: globals.css had no :focus-visible at all.
    expect(baselineRules()).toHaveLength(1);
  });

  it("F2 fence: the baseline sits inside `@layer base`", () => {
    // ⚠ THE LOAD-BEARING ASSERTION.
    //
    // Measured from compiled output: Tailwind v4 emits `.focus\:outline-none`
    // into `@layer utilities`, and a user `@layer base { }` block joins the
    // pre-declared `base` layer. Layer order beats specificity
    // unconditionally, so utilities win and every call site that opts out
    // keeps its own treatment.
    //
    // Hoist this rule out of the layer and it becomes UNLAYERED, which beats
    // EVERY layered rule in the document — silently re-painting an outline on
    // top of all ~66 deliberate opt-outs and inverting the entire design.
    // That mutant looks like a tidy-up in review. This is what catches it.
    const [rule] = baselineRules();
    const layers = enclosingAtRules(rule).filter((a) => a.name === "layer");
    expect(layers.map((a) => a.params)).toContain("base");
  });

  it("F3 fence: the selector is exactly `:focus-visible`, specificity (0,1,0)", () => {
    // Belt to F2's braces. Even if layer ordering were ever disturbed, a
    // (0,1,0) selector still loses to `.focus\:outline-none:focus` (0,2,0)
    // on specificity alone. Do not "strengthen" this selector.
    const [rule] = baselineRules();
    expect(rule.selector.trim()).toBe(":focus-visible");
  });

  it("F4 fence: it paints a real outline of at least 2px", () => {
    // Kills "rule kept, defanged to outline: none" and "outline: 1px".
    const [rule] = baselineRules();
    const outline = declOf(rule, "outline") ?? "";
    expect(outline).toMatch(/\bsolid\b/);
    const width = outline.match(/(\d+(?:\.\d+)?)px/);
    expect(width, `no px width in outline: ${outline}`).not.toBeNull();
    expect(Number(width![1])).toBeGreaterThanOrEqual(2);
    expect(outline).not.toMatch(/\b(none|hidden)\b/);
  });

  it("F5 fence: the outline colour is a token, never a literal", () => {
    // check-design-tokens.sh scans .ts/.tsx/.js/.jsx only, so a hex literal
    // in globals.css is UNPOLICED by CI. This is that check, for this rule.
    const [rule] = baselineRules();
    const outline = declOf(rule, "outline") ?? "";
    expect(outline).toMatch(/var\(--color-focus\)/);
    expect(outline).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });

  it("F6 fence: a non-zero outline-offset keeps the indicator off the glyphs", () => {
    const [rule] = baselineRules();
    const offset = declOf(rule, "outline-offset");
    expect(offset).toBeDefined();
    expect(offset).not.toMatch(/^0(px)?$/);
  });

  it("F7: the focus token is minted in both tiers", () => {
    // The file's existing two-tier pattern: raw --theme-* in :root, aliased
    // --color-* in @theme. The @theme entry is what mints the
    // `outline-focus` utility that check-design-tokens.sh's phantom-token
    // check requires before any className may reference it.
    expect(customProp(":root", "--theme-focus")).toBeDefined();
    let themed: string | undefined;
    sheet.walkAtRules("theme", (at) => {
      at.walkDecls("--color-focus", (d) => {
        themed = d.value.trim();
      });
    });
    expect(themed, "--color-focus missing from @theme").toBeDefined();
    expect(themed).toMatch(/var\(--theme-focus\)/);
  });

  it("F8 fence: nothing in this file disables an outline", () => {
    // Closes the "defeat the baseline from CSS instead of from a class" door.
    const offenders: string[] = [];
    sheet.walkDecls(/^outline(-style)?$/, (d) => {
      if (/^\s*(none|hidden|0)\s*$/.test(d.value)) {
        offenders.push(`${d.parent && (d.parent as postcss.Rule).selector}: ${d.prop}: ${d.value}`);
      }
    });
    expect(offenders).toEqual([]);
  });
});

describe("TBD-319 Part 1b: the app chrome keeps a non-theming focus colour", () => {
  /** The rule that re-points --color-focus for the always-navy chrome. */
  function chromeRule(): postcss.Rule | undefined {
    let found: postcss.Rule | undefined;
    sheet.walkRules((rule) => {
      let touches = false;
      rule.walkDecls("--color-focus", () => {
        touches = true;
      });
      if (touches && rule.selector.trim() !== ":root") found = rule;
    });
    return found;
  }

  it("F9 fence: a chrome rule re-points --color-focus", () => {
    // Measured: the app chrome is navy in BOTH themes, but --theme-accent in
    // the light theme is the DARKENED brass (#8a6a1f) tuned for white
    // surfaces. Against the active nav item's brass-12% tint over navy
    // (#232f3c) that is 2.70:1 — a WCAG 1.4.11 FAILURE on the item a
    // keyboard user lands on most. The always-bright brass reads 6.06:1.
    //
    // This is compliance, not polish. It was proposed as optional and the
    // measurement says it is not.
    expect(chromeRule(), "no rule re-points --color-focus").toBeDefined();
  });

  it("F10 fence: the chrome's focus colour does NOT theme-switch", () => {
    // ⚠ THE ASSERTION THAT MAKES THIS BEHAVIOURAL RATHER THAN STRUCTURAL.
    //
    // Do not assert the override resolves to a particular hex — that only
    // notices an edit. Assert the PROPERTY: whatever token the chrome points
    // at must have the SAME value in :root and in [data-theme="light"].
    // That is what "the navy chrome's focus colour is theme-independent"
    // means, and it kills the mutant nothing else catches: someone
    // "simplifying" the override back to the plain accent, whose two
    // definitions differ (#D4A64A vs #8a6a1f) and which therefore
    // reintroduces the 2.70:1 failure while looking tidier.
    const rule = chromeRule()!;
    const value = declOf(rule, "--color-focus")!;
    const referenced = value.match(/var\(\s*(--[\w-]+)\s*\)/);
    expect(referenced, `chrome focus colour is not a var(): ${value}`).not.toBeNull();

    // Resolve one alias hop (--color-x: var(--theme-x)) to reach the raw token.
    let token = referenced![1];
    const alias = customProp(":root", token);
    if (alias && /^var\(/.test(alias)) {
      token = alias.match(/var\(\s*(--[\w-]+)\s*\)/)![1];
    } else {
      let themeAlias: string | undefined;
      sheet.walkAtRules("theme", (at) => {
        at.walkDecls(token, (d) => {
          themeAlias = d.value.trim();
        });
      });
      if (themeAlias && /^var\(/.test(themeAlias)) {
        token = themeAlias.match(/var\(\s*(--[\w-]+)\s*\)/)![1];
      }
    }

    const dark = customProp(":root", token);
    const light = customProp('[data-theme="light"]', token);
    expect(dark, `${token} not defined in :root`).toBeDefined();
    expect(light, `${token} not defined in [data-theme="light"]`).toBeDefined();
    expect(
      light,
      `The app chrome is navy in both themes, so its focus colour must not ` +
        `theme-switch. ${token} is ${dark} in :root but ${light} in the light ` +
        `theme, which puts the focused active nav item at 2.70:1 (WCAG 1.4.11 ` +
        `needs 3:1).`,
    ).toBe(dark);
  });

  it("F11 fence: the CSS hook is actually emitted, on the right element, once", () => {
    // ⚠ Derived from the CSS, NOT from a literal in this test. Two
    // independent existence checks against a hardcoded string would pass if
    // the attribute landed on the WRONG element — e.g. on a <button> inside
    // the sidebar, or on the mobile drawer but not the desktop <aside> —
    // while half the chrome rendered the theming brass at 2.70:1, build
    // green. Checking the two sides against EACH OTHER means a one-sided
    // rename fails and a coordinated rename needs no edit here.
    const selector = chromeRule()!.selector.trim();
    const attr = selector.match(/\[([\w-]+)\s*=\s*["']([^"']+)["']\]/);
    expect(attr, `chrome selector is not a [name="value"] hook: ${selector}`).not.toBeNull();
    const [, name, value] = attr!;

    const shellPath = path.join(FRONTEND_ROOT, "components", "AppShell.tsx");
    const src = readFileSync(shellPath, "utf8");
    const sf = ts.createSourceFile(shellPath, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

    const hosts: string[] = [];
    const visit = (n: ts.Node): void => {
      if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
        for (const a of n.attributes.properties) {
          if (
            ts.isJsxAttribute(a) &&
            a.name.getText(sf) === name &&
            a.initializer &&
            ts.isStringLiteral(a.initializer) &&
            a.initializer.text === value
          ) {
            hosts.push(n.tagName.getText(sf));
          }
        }
      }
      ts.forEachChild(n, visit);
    };
    visit(sf);

    expect(hosts, `no [${name}="${value}"] in AppShell.tsx`).toHaveLength(1);
    expect(hosts[0]).toBe("aside");
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Part 2 — nobody neutralises the baseline without replacing it
// ─────────────────────────────────────────────────────────────────────────

const SKIP_DIRS = new Set([
  "node_modules",
  ".next",
  "out",
  "out-apex",
  "public",
  "coverage",
  "tests",
  "scripts",
]);

function walk(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry.startsWith(".") || SKIP_DIRS.has(entry)) continue;
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (full.endsWith(".tsx") || full.endsWith(".ts")) out.push(full);
  }
  return out;
}

/** Kills the outline. ⚠ `outline-hidden` is included deliberately: Tailwind
 *  v4 renamed the semantics, so `outline-none` is now `outline-style: none`
 *  while `outline-hidden` is a TRANSPARENT 2px outline (v3's `outline-none`).
 *  A transparent outline is invisible outside forced-colors mode, so it
 *  defeats the baseline just as completely. Today's tree uses `outline-none`
 *  exclusively — a matcher covering only what exists would be green and blind
 *  to the migration that is coming. */
const SUPPRESSOR = /(^|:)outline-(none|hidden|0)$/;

/** A brass replacement. ⚠ Brass ONLY, deliberately. docs/design/DESIGN.md §5 says the
 *  focus state uses Brass Tally. Accepting `focus:bg-surface-raised` would
 *  bless three call sites that believe a background shift is a focus
 *  indicator; measured, surface -> surface-raised is 1.15:1 on dark and
 *  ~1.03:1 on light. It looks like an indicator to the author and is not one. */
const REPLACEMENT =
  /^(focus|focus-visible|focus-within|group-focus|group-focus-visible):(ring-(2|\[?\d)|ring-accent|ring-focus|outline-accent|outline-focus|border-accent|shadow-)/;

/** Programmatic focus targets that are not interactive controls. Strict
 *  equality below: a stale entry fails, so this cannot rot upward.
 *  ⚠ Deliberately NOT a blanket `[tabindex="-1"]` carve-out — that would
 *  auto-exempt a future roving-tabindex menu, which is a real control that
 *  must keep its indicator. */
const ALLOWLIST: { file: string; reason: string }[] = [
  {
    file: "app/accounts/page.tsx",
    reason:
      "<h1 tabIndex={-1}> is a programmatic announce target focused after a " +
      "successful retry (headingRef.current?.focus() at :353), not an " +
      "interactive control. A brass rectangle around the page title on " +
      "recovery would be noise, and it is never reached by Tab.",
  },
  {
    file: "components/transactions/TagChipInput.tsx",
    reason:
      "The chip input's indicator is drawn by its WRAPPER's focus-within: " +
      "ring, so an outline on the bare inner input would double-draw inside " +
      "it. The wrapper is the control the user perceives.",
  },
];

function classStrings(file: string): { text: string; line: number }[] {
  const src = readFileSync(file, "utf8");
  if (!/outline-(none|hidden|0)/.test(src)) return [];
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

function suppressionOffences(file: string): { file: string; line: number; text: string }[] {
  const out: { file: string; line: number; text: string }[] = [];
  for (const { text, line } of classStrings(file)) {
    const tokens = text.split(/\s+/).filter(Boolean);
    if (!tokens.some((t) => SUPPRESSOR.test(t))) continue;
    if (tokens.some((t) => REPLACEMENT.test(t))) continue;
    out.push({ file: path.relative(FRONTEND_ROOT, file), line, text: text.trim().slice(0, 100) });
  }
  return out;
}

describe("TBD-319 Part 2: no call site removes the baseline without replacing it", () => {
  const scanned = walk(FRONTEND_ROOT);

  it("F12 fence: every `outline-none` supplies a brass replacement", () => {
    // ⚠ The ticket's DoD asks for "a test that catches the eleventh site".
    // Under this design there IS no eleventh site: a new link needs no class
    // at all, it inherits. The recurrence risk INVERTS — it becomes a
    // contributor who writes `outline-none` to silence the new outline on
    // their dropdown and supplies nothing. That is what this catches.
    const offenders = scanned
      .flatMap(suppressionOffences)
      .filter((o) => !ALLOWLIST.some((a) => a.file === o.file));

    expect(
      offenders.map((o) => `${o.file}:${o.line}  ${o.text}`),
      "These remove the global brass focus outline and supply nothing " +
        "equivalent. A background shift is NOT a focus indicator " +
        "(surface -> surface-raised is 1.15:1). Either drop the " +
        "`outline-none` and inherit the baseline, or add a brass " +
        "replacement (`focusInset` from lib/styles.ts for a wrapper whose " +
        "outline would be clipped).",
    ).toEqual([]);
  });

  it("F13: the allowlist has no stale entries", () => {
    // Strict equality per the repo's ceiling rule: an allowlist that can rot
    // upward is an allowlist that dies without telling you.
    const live = ALLOWLIST.filter((a) =>
      scanned.some(
        (f) => path.relative(FRONTEND_ROOT, f) === a.file && suppressionOffences(f).length > 0,
      ),
    );
    expect(live).toHaveLength(ALLOWLIST.length);
  });

  it("F14 fence: global-error.tsx paints a REAL outline on keyboard focus", () => {
    // ⚠ app/global-error.tsx deliberately loads NO globals.css so it can
    // render when the CSS pipeline is the thing that broke -- and
    // check-design-tokens.sh excludes it for that reason. So the baseline
    // cannot reach it, and its two controls painted the UA default.
    //
    // A fence certifying "this app has a token-based focus baseline" while
    // one page is silently exempt guarantees a property the app does not
    // have. This is that page's obligation, asserted rather than written in
    // a PR body where it is unfindable in six months.
    //
    // ⚠ THIS PARSES. The first version of this test asserted the source
    // merely CONTAINED ":focus-visible" and "outline" -- and deleting the
    // whole outline assignment left it GREEN, because `matches(":focus-
    // visible")` and `style.outlineOffset` still matched those substrings.
    // Caught by the injection run, not by review. An existence check on a
    // substring is not a fence on behaviour.
    const file = path.join(FRONTEND_ROOT, "app", "global-error.tsx");
    const src = readFileSync(file, "utf8");
    const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

    // (a) something assigns a real outline VALUE -- a width and a colour.
    const painted: string[] = [];
    const visit = (n: ts.Node): void => {
      if (
        ts.isBinaryExpression(n) &&
        n.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
        ts.isPropertyAccessExpression(n.left) &&
        n.left.name.text === "outline" &&
        (ts.isStringLiteral(n.right) || ts.isNoSubstitutionTemplateLiteral(n.right))
      ) {
        painted.push(n.right.text);
      }
      ts.forEachChild(n, visit);
    };
    visit(sf);

    const real = painted.filter((v) => /\d+px/.test(v) && /#[0-9a-fA-F]{3,8}/.test(v));
    expect(
      real,
      "global-error.tsx must assign a real outline (width + colour) on focus; " +
        `found assignments: ${JSON.stringify(painted)}`,
    ).not.toHaveLength(0);

    // (b) it is gated on :focus-visible, so mouse clicks stay quiet.
    //
    // ⚠ PARSED, not grepped -- and this is not pedantry. The first version
    // asserted `src` matched /matches\(":focus-visible"\)/, and deleting the
    // real guard left it GREEN because the phrase also appears in the
    // explanatory comment fifteen lines above it. A grep can be satisfied by
    // a comment; that is a standing rule in this repo and this fence broke it.
    const gates: string[] = [];
    const visit3 = (n: ts.Node): void => {
      if (
        ts.isCallExpression(n) &&
        ts.isPropertyAccessExpression(n.expression) &&
        n.expression.name.text === "matches" &&
        n.arguments.length === 1 &&
        ts.isStringLiteral(n.arguments[0])
      ) {
        gates.push((n.arguments[0] as ts.StringLiteral).text);
      }
      ts.forEachChild(n, visit3);
    };
    visit3(sf);
    expect(
      gates,
      "the focus handler must gate on :focus-visible so a mouse click does " +
        "not paint an outline",
    ).toContain(":focus-visible");

    // (c) BOTH controls are wired -- not just whichever one was easiest.
    const wired: string[] = [];
    const visit2 = (n: ts.Node): void => {
      if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
        const tag = n.tagName.getText(sf);
        if (tag === "button" || tag === "a") {
          const names = n.attributes.properties
            .filter(ts.isJsxAttribute)
            .map((a) => a.name.getText(sf));
          if (names.includes("onFocus") && names.includes("onBlur")) wired.push(tag);
        }
      }
      ts.forEachChild(n, visit2);
    };
    visit2(sf);
    expect(wired.sort()).toEqual(["a", "button"]);
  });
});

describe("TBD-319: anti-vacuity", () => {
  const scanned = walk(FRONTEND_ROOT);

  it("the walk reaches the places opt-outs actually live", () => {
    // Guards the guard. An inclusion-list walk would make any coverage claim
    // circular; this one starts at the frontend root and excludes. Naming
    // lib/styles.ts is the point — the primitives are where suppressions
    // concentrate, and a walk scoped to app/ + components/ would miss every
    // one of them.
    const rel = scanned.map((f) => path.relative(FRONTEND_ROOT, f));
    expect(scanned.length).toBeGreaterThan(50);
    for (const required of [
      "lib/styles.ts",
      "components/AppShell.tsx",
      "app/global-error.tsx",
    ]) {
      expect(rel).toContain(required);
    }
  });

  it("a suppressor hidden in a comment or prose cannot escape the parse", () => {
    // Direct regression test for the defect that killed an earlier gate's
    // regex: `/admin/*` in a line comment opened a block comment and blanked
    // 325 lines of a real file out of the scan. An AST visitor never sees
    // comments at all; string literals it sees as data, which is why the
    // token split (not a substring test) is what decides.
    const hostile = [
      '// Catalog of /admin/* pages. Never write focus:outline-none here.',
      '/* focus:outline-none in a block comment */',
      'const doc = "the class focus:outline-none disables it";',
      'export const ok = "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30";',
      'export const bad = "focus:outline-none text-sm";',
    ].join("\n");
    const sf = ts.createSourceFile("hostile.tsx", hostile, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const strings: string[] = [];
    const visit = (n: ts.Node): void => {
      if (ts.isStringLiteral(n)) strings.push(n.text);
      ts.forEachChild(n, visit);
    };
    visit(sf);

    const offending = strings.filter((s) => {
      const tokens = s.split(/\s+/).filter(Boolean);
      return tokens.some((t) => SUPPRESSOR.test(t)) && !tokens.some((t) => REPLACEMENT.test(t));
    });
    // The prose string mentions the class but as one token among words, so it
    // matches the suppressor; that is honest -- it IS the token. What must
    // NOT happen is the comment forms being seen at all.
    expect(strings).not.toContain(" focus:outline-none in a block comment ");
    expect(offending).toContain("focus:outline-none text-sm");
    expect(offending).not.toContain(
      "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30",
    );
  });

  it("the postcss walker ignores a :focus-visible mentioned only in a comment", () => {
    const probe = postcss.parse("/* :focus-visible { outline: 2px } */ a { color: red }");
    const rules: string[] = [];
    probe.walkRules((r) => {
      rules.push(r.selector);
    });
    expect(rules).toEqual(["a"]);
  });
});
