/**
 * TBD-323: the shared Switch primitive.
 *
 * Every fence names the wrong implementation it kills. Two things NO test in
 * this file can see, stated so nobody mistakes a green run for them:
 *   - LAYOUT. jsdom has no layout, so the 44px target is a class-level GUARD
 *     (T5). The real proof is `getBoundingClientRect()` in a browser at the
 *     visual gate.
 *   - FOCUS SURVIVING A SAVE. jsdom does not implement the HTML focus-fixup
 *     rule, so T4 proves only that a pending switch is not `disabled`. Whether
 *     `document.activeElement` survives a slowed save is verified in the real
 *     browser at the visual gate.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { useState } from "react";
import postcss from "postcss";
import { fireEvent, render, screen } from "@testing-library/react";

import Switch from "@/components/ui/Switch";

function Harness({
  initial,
  label = "Budgets",
  layout,
}: {
  initial: boolean;
  label?: string;
  layout?: "inline" | "stacked";
}) {
  const [checked, setChecked] = useState(initial);
  return (
    <div data-testid="harness">
      <Switch checked={checked} onChange={setChecked} label={label} layout={layout} />
    </div>
  );
}

const STATE_OR_ACTION_NAME = /^(enable|disable|turn on|turn off|enabled|disabled|on|off)\b/i;

describe("TBD-323 Switch: accessible name", () => {
  it("T1 fence: the name is exactly the label, not state/action phrased, and survives a toggle on the same node", () => {
    // Kills: an action-phrased name ("Disable Budgets" fails the EXACT query;
    // a regex like /budgets/i would pass it), a name derived from `checked`
    // (the post-toggle exact query fails), and a remount on toggle
    // (`key={checked}`, which also drops focus: `toBe(sw)` fails).
    render(<Harness initial={true} />);
    const sw = screen.getByRole("switch", { name: "Budgets" });
    expect(sw.getAttribute("aria-label") ?? "").not.toMatch(STATE_OR_ACTION_NAME);
    expect(sw).toHaveAttribute("aria-checked", "true");

    fireEvent.click(sw);

    const after = screen.getByRole("switch", { name: "Budgets" });
    expect(after).toBe(sw);
    expect(after).toHaveAttribute("aria-checked", "false");

    fireEvent.click(after);
    expect(screen.getByRole("switch", { name: "Budgets" })).toBe(sw);
  });
});

describe("TBD-323 Switch: state", () => {
  it("T2 fence: aria-checked mirrors `checked` and onChange receives the NEGATION", () => {
    // Kills: a hardcoded aria-checked, and onChange(checked) (a no-op toggle).
    for (const start of [true, false]) {
      const onChange = vi.fn();
      const { unmount } = render(
        <Switch checked={start} onChange={onChange} label="Forecast" />,
      );
      const sw = screen.getByRole("switch", { name: "Forecast" });
      expect(sw).toHaveAttribute("aria-checked", String(start));
      fireEvent.click(sw);
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith(!start);
      unmount();
    }
  });

  it("T3 fence: the state text follows `checked` in both layouts and is hidden from AT", () => {
    // Kills: inverted or static text, and text dropped in the stacked layout.
    for (const layout of ["inline", "stacked"] as const) {
      const { unmount } = render(<Harness initial={true} layout={layout} />);
      const root = screen.getByTestId("harness");
      const on = screen.getByText("Enabled");
      expect(root.contains(on)).toBe(true);
      // Ruling Q3: state, not label. aria-checked already announces it.
      expect(on.closest("[aria-hidden='true']")).not.toBeNull();
      // ...but hidden from AT ONLY. Kills an `sr-only`/`hidden` state text that
      // satisfies the aria-hidden assertion while no sighted user sees it.
      expect(on).toBeVisible();
      for (let el: HTMLElement | null = on; el && el !== root; el = el.parentElement) {
        expect(el.getAttribute("class") ?? "").not.toMatch(/(^|\s)(sr-only|hidden|invisible|opacity-0)(\s|$)/);
      }
      expect(screen.queryByText("Disabled")).toBeNull();

      fireEvent.click(screen.getByRole("switch", { name: "Budgets" }));
      expect(screen.getByText("Disabled")).toBeTruthy();
      expect(screen.queryByText("Enabled")).toBeNull();
      unmount();
    }
  });
});

describe("TBD-323 Switch: locked vs pending", () => {
  it("T4 fence: locked is real `disabled`; pending is aria-disabled, stays focusable, and ignores clicks", () => {
    // Kills: pending implemented as real `disabled` (drops a keyboard user's
    // focus mid-save via the focus-fixup rule), and a pending switch that still
    // fires onChange (the double save).
    // ⚠ Proves NOT-disabled only. Focus surviving the save is a browser check.
    const locked = vi.fn();
    const { unmount } = render(
      <Switch checked={true} onChange={locked} label="Security email notifications" disabled />,
    );
    const lockedSw = screen.getByRole("switch", { name: "Security email notifications" });
    expect(lockedSw).toBeDisabled();
    fireEvent.click(lockedSw);
    expect(locked).not.toHaveBeenCalled();
    unmount();

    const pending = vi.fn();
    render(<Switch checked={false} onChange={pending} label="Budgets" pending />);
    const sw = screen.getByRole("switch", { name: "Budgets" });
    expect(sw).not.toBeDisabled();
    expect(sw).toHaveAttribute("aria-disabled", "true");
    sw.focus();
    expect(document.activeElement).toBe(sw);
    fireEvent.click(sw);
    fireEvent.click(sw);
    expect(pending).not.toHaveBeenCalled();
  });

  it("T4b: a switch that is neither locked nor pending carries no aria-disabled", () => {
    render(<Switch checked={false} onChange={vi.fn()} label="Budgets" />);
    expect(screen.getByRole("switch", { name: "Budgets" })).not.toHaveAttribute("aria-disabled");
  });
});

function tokens(el: Element): string[] {
  return (el.getAttribute("class") ?? "").split(/\s+/).filter(Boolean);
}

function parts(sw: HTMLElement) {
  const track = sw.querySelector(":scope > span");
  const knob = track?.querySelector(":scope > span");
  expect(track, "switch has no track span").not.toBeNull();
  expect(knob, "track has no knob span").not.toBeNull();
  return { track: track!, knob: knob! };
}

describe("TBD-323 Switch: visual invariants", () => {
  it("T5a fence: the track tokens are pinned, success on and border-strong off", () => {
    // Kills: an ON `bg-accent` (a lit brass track on every load breaks The One
    // Brass Rule, and T6 cannot see it: brass clears 3:1) and an OFF track
    // swapped to another >= 3:1 token such as `bg-text-muted`.
    for (const [checked, token] of [
      [true, "bg-success"],
      [false, "bg-border-strong"],
    ] as const) {
      const { unmount } = render(<Switch checked={checked} onChange={vi.fn()} label="Budgets" />);
      const { track } = parts(screen.getByRole("switch", { name: "Budgets" }));
      expect(tokens(track).filter((t) => t.startsWith("bg-"))).toEqual([token]);
      unmount();
    }
  });

  it("T5b guard: the switch is type=button, so it never submits a surrounding form", () => {
    render(
      <form>
        <Switch checked onChange={vi.fn()} label="Budgets" />
      </form>,
    );
    expect(screen.getByRole("switch", { name: "Budgets" })).toHaveAttribute("type", "button");
  });

  it("T5 guard: 44px box, no knob shadow, no raw white or black knob", () => {
    // ⚠ GUARD, not a fence on layout: proves the classes, not the rendered
    // size. The 44px proof is the browser measurement at the visual gate.
    render(<Switch checked={true} onChange={vi.fn()} label="Budgets" />);
    const sw = screen.getByRole("switch", { name: "Budgets" });
    expect(tokens(sw)).toEqual(expect.arrayContaining(["h-11", "w-11"]));
    const { knob, track } = parts(sw);
    for (const t of [...tokens(knob), ...tokens(track)]) {
      expect(t).not.toMatch(/^shadow/);
      expect(t).not.toMatch(/^bg-(white|black)\b/);
    }
  });

  it("T6 fence: track vs surface and knob vs track are >= 3:1 in both states and both themes (WCAG 1.4.11)", () => {
    // Kills: the off track `bg-border` (1.35 dark / 1.31 light), the TBD-197
    // surface knob on that track, a `bg-white` knob (not a theme token), and a
    // later retune of --theme-border-strong below 3:1.
    // ⚠ Ceiling: asserted against `surface` ONLY. The Switch is documented as
    // surface-only; border-strong is 2.89:1 on surface-raised (dark) and
    // 2.96:1 on bg (light).
    const css = readFileSync(path.resolve(__dirname, "..", "..", "..", "app", "globals.css"), "utf8");
    const sheet = postcss.parse(css);
    const themeValue = (selector: string, name: string): string | undefined => {
      let v: string | undefined;
      sheet.walkRules((rule) => {
        if (rule.selector.trim() !== selector) return;
        rule.walkDecls(`--theme-${name}`, (d) => {
          v = d.value.trim();
        });
      });
      return v;
    };
    const resolve = (name: string, theme: "dark" | "light"): string => {
      const light = theme === "light" ? themeValue('[data-theme="light"]', name) : undefined;
      const value = light ?? themeValue(":root", name);
      expect(value, `bg-${name} is not a theme token`).toBeDefined();
      expect(value, `--theme-${name} is not a hex`).toMatch(/^#[0-9a-fA-F]{6}$/);
      return value!;
    };
    const lum = (hex: string) => {
      const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
      const l = c.map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
      return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
    };
    const ratio = (a: string, b: string) => {
      const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    const bgToken = (el: Element) => {
      const bg = tokens(el).filter((t) => t.startsWith("bg-"));
      expect(bg, `expected exactly one bg-* token, got ${bg}`).toHaveLength(1);
      return bg[0].slice(3);
    };

    for (const checked of [true, false]) {
      const { unmount } = render(<Switch checked={checked} onChange={vi.fn()} label="Budgets" />);
      const { track, knob } = parts(screen.getByRole("switch", { name: "Budgets" }));
      const trackName = bgToken(track);
      const knobName = bgToken(knob);
      for (const theme of ["dark", "light"] as const) {
        const surface = resolve("surface", theme);
        const t = resolve(trackName, theme);
        const k = resolve(knobName, theme);
        const where = `${theme} theme, ${checked ? "on" : "off"}`;
        expect(ratio(t, surface), `track bg-${trackName} vs surface, ${where}`).toBeGreaterThanOrEqual(3);
        expect(ratio(k, t), `knob bg-${knobName} vs track bg-${trackName}, ${where}`).toBeGreaterThanOrEqual(3);
      }
      unmount();
    }
  });

  it("T7 fence: the switch suppresses no focus outline and paints no ring of its own", () => {
    // Kills: copying TBD-197's `focus:outline-none focus-visible:ring-accent/30`
    // (about 1.78:1 dark / 1.50:1 light). TBD-319's F12 ACCEPTS that string as
    // a replacement, so it would not catch this (TBD-521).
    render(<Switch checked={false} onChange={vi.fn()} label="Budgets" pending />);
    const sw = screen.getByRole("switch", { name: "Budgets" });
    const all = [sw, ...Array.from(sw.querySelectorAll("*"))].flatMap(tokens);
    for (const t of all) {
      // Bare and variant-prefixed (focus:, focus-visible:, md:focus:, ...).
      expect(t).not.toMatch(/(^|:)outline-(none|hidden|0|transparent)$/);
      expect(t).not.toMatch(/(^|:)ring(-|$)/);
    }
  });
});
