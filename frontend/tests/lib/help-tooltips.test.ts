/**
 * Content-map sanity tests (L5.3).
 *
 * The tooltip content map ships customer-facing copy in a typed
 * record so future edits are one-liners. These tests lock the
 * invariants the codebase relies on:
 *   - No em-dashes anywhere (house style, user-locked).
 *   - Every entry has non-empty content.
 *   - getHelpTooltip throws in dev when a key is unknown.
 *   - The full set covers the ~15 frequently-confused fields the spec
 *     calls out (sanity floor, not exact count).
 */
import { describe, expect, it } from "vitest";

import {
  HELP_TOOLTIPS,
  getHelpTooltip,
  type HelpTooltipKey,
} from "@/lib/help/tooltips";

describe("HELP_TOOLTIPS", () => {
  const entries = Object.entries(HELP_TOOLTIPS) as [
    HelpTooltipKey,
    (typeof HELP_TOOLTIPS)[HelpTooltipKey],
  ][];

  it("contains at least 15 entries (L5.3 floor)", () => {
    expect(entries.length).toBeGreaterThanOrEqual(15);
  });

  it("every entry has non-empty content", () => {
    for (const [key, entry] of entries) {
      expect(entry.content, `key=${key}`).toBeTruthy();
      expect(entry.content.length, `key=${key}`).toBeGreaterThan(5);
    }
  });

  it("no entry uses an em-dash (house style)", () => {
    for (const [key, entry] of entries) {
      expect(entry.content, `key=${key}`).not.toMatch(/—/);
      if (entry.triggerLabel) {
        expect(entry.triggerLabel, `key=${key}`).not.toMatch(/—/);
      }
    }
  });

  it("no entry uses an en-dash inside a sentence (house style)", () => {
    // En-dashes between words ("X – Y") read like em-dashes; allow
    // them only when they border digits (date/number ranges).
    for (const [key, entry] of entries) {
      const offending = entry.content.match(/\D\s–\s\D/);
      expect(offending, `key=${key}`).toBeNull();
    }
  });

  it("getHelpTooltip returns the matching entry", () => {
    const first = entries[0];
    if (!first) throw new Error("HELP_TOOLTIPS is empty");
    const [key, entry] = first;
    expect(getHelpTooltip(key)).toEqual(entry);
  });

  it("getHelpTooltip throws in dev when key is unknown", () => {
    // @ts-expect-error -- testing the runtime guard
    expect(() => getHelpTooltip("definitely-not-a-key")).toThrow(
      /unknown key/,
    );
  });

  it("covers the Reports aggregation jargon (Reports v2 polish)", () => {
    const aggKeys: HelpTooltipKey[] = [
      "reports.agg.sum",
      "reports.agg.count",
      "reports.agg.avg",
      "reports.agg.distinct",
    ];
    for (const k of aggKeys) {
      expect(HELP_TOOLTIPS[k], `key=${k}`).toBeTruthy();
    }
    // Distinct copy mentions counting unique values (spec example).
    expect(HELP_TOOLTIPS["reports.agg.distinct"].content).toMatch(/unique/i);
    // Master-category explainer is present for the dimension labels.
    expect(HELP_TOOLTIPS["reports.master-category"]).toBeTruthy();
  });

  it("covers the surfaces the L5.3 spec calls out", () => {
    // Per the L5.3 ticket: transactions (sign convention, frequency),
    // categories (type), budgets (recurrence). Lock those keys here
    // so accidentally dropping one shows up as a test failure rather
    // than a silent regression.
    const required: HelpTooltipKey[] = [
      "tx.type",
      "tx.frequency",
      "cat.type",
      "budget.monthly-limit",
    ];
    for (const k of required) {
      expect(HELP_TOOLTIPS[k]).toBeTruthy();
    }
  });
});
