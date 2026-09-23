/**
 * TBD-431. The reorder control makes ``useSeriesQueries``' SWR key
 * order-sensitive over the exact same data: without a canonical key, moving
 * a series in the UI looks like a brand-new query set and refetches
 * (skeleton flash, wasted round-trip, and a 60/minute limiter that a 5-series
 * widget can exhaust in 13 moves). These fence the canonical-order helpers
 * directly, with no render and no SWR, so they run outside the act() gate.
 */
import {
  canonicalizeQueryOrder,
  canonicalSeriesKey,
  remapCanonicalResults,
} from "@/lib/reports/useReportQuery";
import type { Measure, MeasureField, ReportsQuery } from "@/lib/reports/types";

function q(
  field: MeasureField,
  agg: Measure["agg"] = "sum",
  filters: ReportsQuery["filters"] = [],
): ReportsQuery {
  return {
    dataset: "transactions",
    measure: { agg, field } as Measure,
    dimensions: ["month"],
    filters,
  };
}

describe("canonical series-query ordering (TBD-431)", () => {
  it("fence reorder-does-not-change-swr-key: a pure reorder keys identically", () => {
    const original = [q("amount"), q("balance"), q("net_amount")];
    const reordered = [original[1], original[0], original[2]];
    expect(canonicalSeriesKey(reordered)).toEqual(canonicalSeriesKey(original));
  });

  it("fence measure-change-does-change-swr-key: swapping a measure changes the key", () => {
    const original = [q("amount"), q("balance"), q("net_amount")];
    const changed = [q("amount"), q("balance"), q("id")];
    expect(canonicalSeriesKey(changed)).not.toEqual(canonicalSeriesKey(original));
  });

  // ⚠ Varying the FIELD alone does not fence over-canonicalisation. A key
  // built from the sorted field names only -- dropping agg and filters --
  // passes the test above, and then changing sum to avg leaves the key
  // unchanged and SWR serves STALE data under the new label. That is the
  // wrong-number class, so each component of the query gets its own case.
  it("fence measure-change-does-change-swr-key: agg alone changes the key", () => {
    const original = [q("amount", "sum"), q("balance")];
    const changed = [q("amount", "avg"), q("balance")];
    expect(canonicalSeriesKey(changed)).not.toEqual(canonicalSeriesKey(original));
  });

  it("fence measure-change-does-change-swr-key: filters alone change the key", () => {
    const original = [q("amount"), q("balance")];
    const changed = [
      q("amount", "sum", [{ field: "txn_type", op: "eq", value: "expense" }]),
      q("balance"),
    ];
    expect(canonicalSeriesKey(changed)).not.toEqual(canonicalSeriesKey(original));
  });

  it("fence measure-change-does-change-swr-key: dimensions alone change the key", () => {
    const original = [q("amount")];
    const changed = [{ ...q("amount"), dimensions: ["day"] } as ReportsQuery];
    expect(canonicalSeriesKey(changed)).not.toEqual(canonicalSeriesKey(original));
  });

  it("fence reorder-remaps-series-to-display-order: results land back at the moved index", () => {
    // ⚠ The permutation must be a 3-CYCLE. An identity permutation makes the
    // remap a no-op and the fence blind to every mapping error; a 2-element
    // transposition is its OWN INVERSE, so it cannot tell the correct
    // direction from the reversed one. Aggs avg < count < sum sort the
    // display order [sum, avg, count] to canonical [avg, count, sum], i.e.
    // order = [1,2,0] -- a genuine 3-cycle whose inverse [2,0,1] differs.
    const displayQueries = [
      q("net_amount", "sum"),
      q("amount", "avg"),
      q("id", "count"),
    ];
    expect(canonicalizeQueryOrder(displayQueries)).toEqual([1, 2, 0]);
    const order = canonicalizeQueryOrder(displayQueries);
    const canonicalQueries = order.map((i) => displayQueries[i]);
    // The "canonical result" for a query is just a marker tied to which
    // display slot the query came from, so we can verify the remap by value.
    const canonicalResults = canonicalQueries.map(
      (cq) => `result-for-${cq.measure.field}`,
    );
    const displayResults = remapCanonicalResults(canonicalResults, order);
    // Each display slot must hold ITS OWN measure's result. Written out
    // literally rather than derived from displayQueries, or the assertion
    // would recompute the very mapping it is meant to check.
    expect(displayResults).toEqual([
      "result-for-net_amount",
      "result-for-amount",
      "result-for-id",
    ]);
  });

  it("fence duplicate-measure-pairs-survive-canonicalisation: both survive, none dropped", () => {
    const displayQueries = [q("amount"), q("amount"), q("balance")];
    const order = canonicalizeQueryOrder(displayQueries);
    expect(order).toHaveLength(3);
    expect([...order].sort()).toEqual([0, 1, 2]);
    const canonicalQueries = order.map((i) => displayQueries[i]);
    const canonicalResults = canonicalQueries.map((_, i) => `r${i}`);
    const displayResults = remapCanonicalResults(canonicalResults, order);
    expect(displayResults).toHaveLength(3);
    expect(displayResults.every((r) => r !== undefined)).toBe(true);
  });
});
