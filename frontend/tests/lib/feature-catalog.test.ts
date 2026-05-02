import { describe, expect, it } from "vitest";

import { FEATURE_LABELS } from "@/lib/feature-catalog";
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
});
