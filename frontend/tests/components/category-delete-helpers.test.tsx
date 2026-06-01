import { describe, expect, it } from "vitest";

import { ApiResponseError } from "@/lib/api";
import {
  buildFailure,
  compatibleTargets,
} from "@/components/categories/categoryDeleteHelpers";
import type { Category } from "@/lib/types";

const sub: Category = {
  id: 101,
  name: "Restaurants",
  type: "expense",
  parent_id: 100,
  parent_name: "Food",
  description: null,
  slug: null,
  is_system: false,
  transaction_count: 5,
};

function apiErr(status: number, detail: unknown): ApiResponseError {
  return new ApiResponseError(status, "x", undefined, detail);
}

describe("buildFailure message fallbacks", () => {
  it("last_in_type with type+scope renders the specific message", () => {
    const f = buildFailure(
      sub,
      apiErr(409, { detail: "last_in_type", type: "expense", scope: "subcategory" }),
    );
    expect(f.reason).toBe("Cannot delete the only expense subcategory.");
    expect(f.reason).not.toContain("  ");
  });

  it("last_in_type without type/scope renders a clean message (no double spaces)", () => {
    const f = buildFailure(sub, apiErr(409, { detail: "last_in_type" }));
    expect(f.reason_code).toBe("last_in_type");
    expect(f.reason).not.toContain("  ");
    expect(f.reason.trim()).toBe(f.reason);
    expect(f.reason).toBe("Cannot delete the only category of its type.");
  });

  it("type_mismatch with types renders the specific message", () => {
    const f = buildFailure(
      sub,
      apiErr(400, {
        detail: "type_mismatch",
        source_type: "income",
        target_type: "expense",
      }),
    );
    expect(f.reason).toContain("expense");
    expect(f.reason).toContain("income");
    expect(f.reason).not.toContain("  ");
  });

  it("type_mismatch without types renders a generic fallback (no blanks)", () => {
    const f = buildFailure(sub, apiErr(400, { detail: "type_mismatch" }));
    expect(f.reason_code).toBe("type_mismatch");
    expect(f.reason).not.toContain("  ");
    expect(f.reason).toBe(
      "Migration target type is not compatible with this category.",
    );
  });
});

describe("compatibleTargets", () => {
  const cats: Category[] = [
    { id: 100, name: "Food", type: "expense", parent_id: null, parent_name: null, description: null, slug: "food", is_system: true, transaction_count: 0 },
    { id: 300, name: "Income", type: "income", parent_id: null, parent_name: null, description: null, slug: "income", is_system: true, transaction_count: 0 },
    { id: 500, name: "Mixed", type: "both", parent_id: null, parent_name: null, description: null, slug: null, is_system: false, transaction_count: 0 },
    { id: 101, name: "Restaurants", type: "expense", parent_id: 100, parent_name: "Food", description: null, slug: null, is_system: false, transaction_count: 5 },
  ];

  it("excludes the source, non-masters, and excludeIds; matches by type", () => {
    const out = compatibleTargets(sub, cats);
    const ids = out.map((c) => c.id).sort();
    // expense source -> expense (Food) or both (Mixed) masters
    expect(ids).toEqual([100, 500]);
  });

  it("honors excludeIds", () => {
    const out = compatibleTargets(sub, cats, new Set([100]));
    expect(out.map((c) => c.id)).toEqual([500]);
  });
});
