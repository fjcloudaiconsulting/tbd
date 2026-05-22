/**
 * Tour constants tests (L5.3).
 *
 * Locks the shape of the cross-page tour: every extended step has
 * matching copy in STEP_COPY, every prefix in EXTENDED_TOUR_STEPS
 * routes to a known surface, and the sessionStorage flag values stay
 * distinct so the dashboard auto-start can tell which tour to launch.
 */
import { describe, expect, it } from "vitest";

import {
  DASHBOARD_TOUR_STEPS,
  EXTENDED_TOUR_STEPS,
  STEP_COPY,
  TOUR_FLAG_KEY,
  TOUR_FLAG_VALUE_DASHBOARD,
  TOUR_FLAG_VALUE_EXTENDED,
  pagePrefix,
  routeForPrefix,
} from "@/lib/help/tour";

describe("tour constants", () => {
  it("the sessionStorage key matches the legacy literal exactly", () => {
    // The wizard auto-start guard reads this key. Renaming it would
    // strand any partially-onboarded user with a flag they no longer
    // satisfy. Lock the value.
    expect(TOUR_FLAG_KEY).toBe("tbd-pending-dashboard-tour");
  });

  it("the dashboard and extended flag values are distinct", () => {
    expect(TOUR_FLAG_VALUE_DASHBOARD).not.toBe(TOUR_FLAG_VALUE_EXTENDED);
    expect(TOUR_FLAG_VALUE_DASHBOARD.length).toBeGreaterThan(0);
    expect(TOUR_FLAG_VALUE_EXTENDED.length).toBeGreaterThan(0);
  });

  it("dashboard tour has at least 5 steps and stays in /dashboard", () => {
    expect(DASHBOARD_TOUR_STEPS.length).toBeGreaterThanOrEqual(5);
    for (const id of DASHBOARD_TOUR_STEPS) {
      expect(pagePrefix(id)).toBe("dashboard");
    }
  });

  it("extended tour covers 5-7 surfaces (spec requirement)", () => {
    expect(EXTENDED_TOUR_STEPS.length).toBeGreaterThanOrEqual(5);
    expect(EXTENDED_TOUR_STEPS.length).toBeLessThanOrEqual(7);
  });

  it("every extended step has copy in STEP_COPY", () => {
    for (const id of EXTENDED_TOUR_STEPS) {
      expect(STEP_COPY[id], `missing copy for ${id}`).toBeTruthy();
      expect(STEP_COPY[id]?.title).toBeTruthy();
      expect(STEP_COPY[id]?.body).toBeTruthy();
    }
  });

  it("every dashboard step has copy in STEP_COPY", () => {
    for (const id of DASHBOARD_TOUR_STEPS) {
      expect(STEP_COPY[id], `missing copy for ${id}`).toBeTruthy();
    }
  });

  it("every extended step routes to a known surface", () => {
    for (const id of EXTENDED_TOUR_STEPS) {
      const route = routeForPrefix(pagePrefix(id));
      expect(route, `no route for ${id}`).toBeTruthy();
      expect(route).toMatch(/^\//);
    }
  });

  it("no copy uses an em-dash (house style)", () => {
    for (const [id, copy] of Object.entries(STEP_COPY)) {
      expect(copy.title, `id=${id}`).not.toMatch(/—/);
      expect(copy.body, `id=${id}`).not.toMatch(/—/);
    }
  });

  it("pagePrefix returns the dot-prefix portion", () => {
    expect(pagePrefix("transactions.title")).toBe("transactions");
    expect(pagePrefix("dashboard.header")).toBe("dashboard");
    expect(pagePrefix("noprefix")).toBe("noprefix");
  });

  it("routeForPrefix returns null for unknown prefixes", () => {
    expect(routeForPrefix("nonsense")).toBeNull();
  });
});
