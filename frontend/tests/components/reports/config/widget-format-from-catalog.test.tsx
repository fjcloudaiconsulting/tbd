/**
 * F-8 (TBD-170) — `config.format` must derive from the source catalog.
 *
 * The dormant bug: `SourceMeasure.format` is published by the backend and
 * typed by the frontend, and NOTHING read it. Format was hardcoded
 * `"currency"` at widget creation and never re-derived. It had never fired
 * because every measure shipped to date is currency-shaped — `utilization_pct`
 * is the first `percent` measure in the codebase, so without this a 45%
 * utilization bar renders "€45.00".
 *
 * ⚠ These tests use the measure shape the REAL UI produces. The Field select
 * emits `{...measure, field}` — carrying the PREVIOUS agg over unchanged
 * (SingleMeasureEditor). A test that hand-builds `{agg: "sum", field:
 * "outstanding"}` passes against the buggy `.find(m => m.field === … && m.agg
 * === …)` implementation, because that AST matches the catalog — while the
 * shipped UI can never produce it. That would be a green fence over a live bug.
 */
import { describe, expect, it, vi } from "vitest";

import { buildWidgetMutations } from "@/components/reports/config/useWidgetMutations";
import type { BarConfig, SourceCatalogEntry, Widget } from "@/lib/reports/types";

const CREDIT_UTILIZATION: SourceCatalogEntry = {
  key: "credit_utilization",
  label: "Credit utilization",
  dimensions: [
    { key: "account", label: "Card", kind: "account" },
    { key: "currency", label: "Currency", kind: "currency" },
    { key: "account_active", label: "Status", kind: "boolean" },
  ],
  measures: [
    { key: "utilization_pct", label: "Utilization", agg: "avg", field: "utilization_pct", format: "percent" },
    { key: "outstanding", label: "Outstanding", agg: "sum", field: "outstanding", format: "currency" },
    { key: "credit_limit", label: "Credit limit", agg: "sum", field: "credit_limit", format: "currency" },
    { key: "count_cards", label: "Card count", agg: "count", field: "id", format: "number" },
  ],
  filters: [
    { field: "account_id", label: "Card", ops: ["in"], kind: "account" },
    { field: "currency", label: "Currency", ops: ["eq", "in"], kind: "currency" },
    { field: "account_active", label: "Status", ops: ["eq"], kind: "boolean" },
  ],
} as unknown as SourceCatalogEntry;

const ACCOUNTS: SourceCatalogEntry = {
  key: "accounts",
  label: "Accounts",
  dimensions: [{ key: "account", label: "Account", kind: "account" }],
  measures: [
    { key: "sum_balance", label: "Total balance", agg: "sum", field: "balance", format: "currency" },
    { key: "count_accounts", label: "Account count", agg: "count", field: "id", format: "number" },
  ],
  filters: [{ field: "account_id", label: "Account", ops: ["in"], kind: "account" }],
} as unknown as SourceCatalogEntry;

function barWidget(config: Partial<BarConfig>): Widget {
  return {
    id: "w1",
    type: "bar",
    title: "W",
    config: {
      dataset: "accounts",
      dimensions: ["account"],
      measure: { agg: "sum", field: "balance" },
      filters: {},
      format: "currency",
      ...config,
    },
  } as unknown as Widget;
}

function captureUpdate() {
  const calls: Widget[] = [];
  return { calls, onUpdate: vi.fn((w: Widget) => calls.push(w)) };
}

describe("config.format derives from the source catalog", () => {
  it("switching to credit_utilization makes the widget render percent, not currency", () => {
    const { calls, onUpdate } = captureUpdate();
    const { setDataset } = buildWidgetMutations(
      barWidget({}),
      onUpdate,
      CREDIT_UTILIZATION,
    );
    setDataset("credit_utilization", CREDIT_UTILIZATION);

    const cfg = calls.at(-1)!.config as BarConfig;
    expect(cfg.measure.field).toBe("utilization_pct");
    // Kills the original bug: hardcoded "currency" would render 45% as €45.00.
    expect(cfg.format).toBe("percent");
  });

  it("a RETAINED count(id) measure does not inherit percent", () => {
    // `id` is published by EVERY source, so a count(id) measure survives the
    // dataset switch. An unconditional `entry.measures[0].format` would write
    // "percent" and render 4 cards as "4.0%".
    const { calls, onUpdate } = captureUpdate();
    const widget = barWidget({ measure: { agg: "count", field: "id" } });
    const { setDataset } = buildWidgetMutations(widget, onUpdate, CREDIT_UTILIZATION);
    setDataset("credit_utilization", CREDIT_UTILIZATION);

    const cfg = calls.at(-1)!.config as BarConfig;
    expect(cfg.measure.field).toBe("id");
    expect(cfg.format).toBe("number");
  });

  it("changing the MEASURE within the source re-derives format — using the UI's real shape", () => {
    // ⚠ THE LOAD-BEARING CASE. The Field select emits {...measure, field}, so
    // agg stays "avg" from the utilization measure while the field becomes
    // "outstanding" — which the catalog publishes at "sum".
    //
    // Against the buggy `m.field === f && m.agg === a` lookup this MISSES,
    // falls back to the previous format, and keeps "percent" — rendering
    // €1,234.56 as "1234.6%". Matching on FIELD ONLY is what makes it pass.
    const { calls, onUpdate } = captureUpdate();
    const widget = barWidget({
      dataset: "credit_utilization",
      measure: { agg: "avg", field: "utilization_pct" },
      format: "percent",
    });
    const { setSingleMeasure } = buildWidgetMutations(
      widget,
      onUpdate,
      CREDIT_UTILIZATION,
    );

    // Exactly what SingleMeasureEditor's Field select produces.
    const asTheFieldSelectEmits = {
      ...(widget.config as BarConfig).measure,
      field: "outstanding" as const,
    };
    setSingleMeasure(asTheFieldSelectEmits);

    const cfg = calls.at(-1)!.config as BarConfig;
    expect(cfg.measure).toEqual({ agg: "avg", field: "outstanding" });
    expect(cfg.format).toBe("currency");
  });

  it("switching AWAY from a percent source restores currency", () => {
    const { calls, onUpdate } = captureUpdate();
    const widget = barWidget({
      dataset: "credit_utilization",
      measure: { agg: "avg", field: "utilization_pct" },
      format: "percent",
    });
    const { setDataset } = buildWidgetMutations(widget, onUpdate, ACCOUNTS);
    setDataset("accounts", ACCOUNTS);

    const cfg = calls.at(-1)!.config as BarConfig;
    expect(cfg.format).toBe("currency");
  });

  it("leaves format untouched while the catalog is still loading", () => {
    // `selected` is undefined until /sources resolves; guessing a format then
    // would be worse than leaving the existing one alone.
    const { calls, onUpdate } = captureUpdate();
    const widget = barWidget({ format: "currency" });
    const { setSingleMeasure } = buildWidgetMutations(widget, onUpdate, undefined);
    setSingleMeasure({ agg: "sum", field: "balance" });

    const cfg = calls.at(-1)!.config as BarConfig;
    expect(cfg.format).toBe("currency");
  });
});
