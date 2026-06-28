/**
 * useFilterChipState — auth-readiness gate.
 *
 * Pins the cold-start race fix: the accounts + categories SWR fetches must
 * NOT fire while `useAuth().user` is null (the dashboard mounts this hook
 * above AppShell's auth gate, so firing token-less would 403 on a hard
 * refresh). Once `user` is present the keys go live and both fetch.
 *
 * Revert-resistance: drop the `user ? key : null` guard and the first test
 * fails (a null-user render would fetch immediately).
 */
import React from "react";

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return { ...actual, useAuth: vi.fn() };
});

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn(() => Promise.resolve([])) };
});

import { useFilterChipState } from "@/lib/reports/use-filter-chip-state";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { renderWithSWR, waitFor } from "../../utils/render-with-swr";

function Harness() {
  useFilterChipState(() => {});
  return null;
}

const fetchedPaths = () =>
  vi.mocked(apiFetch).mock.calls.map((c) => c[0]);

describe("useFilterChipState — auth-readiness gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does NOT fetch accounts/categories while user is null", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: null } as never);
    renderWithSWR(<Harness />);
    // Let any (incorrectly) scheduled SWR fetch flush.
    await new Promise((r) => setTimeout(r, 10));
    expect(fetchedPaths()).not.toContain("/api/v1/accounts");
    expect(fetchedPaths()).not.toContain("/api/v1/categories");
  });

  it("fetches accounts + categories once user is present", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: { id: 1 } } as never);
    renderWithSWR(<Harness />);
    await waitFor(() => {
      expect(fetchedPaths()).toContain("/api/v1/accounts");
      expect(fetchedPaths()).toContain("/api/v1/categories");
    });
  });
});
