import { describe, expect, it } from "vitest";

import { FEATURE_LABELS, FEATURE_MODULES, METER_MODULES } from "@/lib/feature-catalog";
import catalog from "../fixtures/feature-catalog.json";

describe("feature-catalog drift guard", () => {
  it("every backend catalog key has a UI label", () => {
    for (const key of catalog.keys) {
      expect(FEATURE_LABELS).toHaveProperty(key);
    }
  });

  it("FEATURE_LABELS contains no orphaned keys", () => {
    for (const key of Object.keys(FEATURE_LABELS)) {
      expect(catalog.keys).toContain(key);
    }
  });

  it("FEATURE_MODULES matches the backend catalog", () => {
    const sorted = Object.fromEntries(
      Object.entries(FEATURE_MODULES).map(([m, keys]) => [m, [...keys].sort()]),
    );
    expect(sorted).toEqual(catalog.modules);
  });

  it("METER_MODULES matches the backend catalog", () => {
    expect(METER_MODULES).toEqual(catalog.meters);
  });

  it("plans is a labelled key in its own module and keys never collide with meters", () => {
    expect(FEATURE_LABELS.plans.label).toBe("Plans");
    expect(FEATURE_MODULES.plans).toEqual(["plans"]);
    const inModules = Object.values(FEATURE_MODULES).flat();
    expect([...inModules].sort()).toEqual([...catalog.keys].sort());
    expect(Object.keys(METER_MODULES).filter((m) => catalog.keys.includes(m))).toEqual([]);
    for (const mod of Object.values(METER_MODULES)) {
      expect(FEATURE_MODULES).toHaveProperty(mod);
    }
  });
});
