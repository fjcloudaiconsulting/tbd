/**
 * CustomDashboard — auth-readiness gate.
 *
 * CustomDashboard renders <AppShell> as a CHILD, so its mount effects run
 * above AppShell's `loading || !user` guard. Without an own gate its layout
 * fetch would fire before AuthProvider restores the token on a hard refresh
 * and 403. These tests pin that gate: no layout fetch while `user` is null
 * (loader shown); the fetch fires once `user` is present.
 *
 * Revert-resistance: remove `if (!authReady) return` (effect) or
 * `loading || !authReady` (render) and the null-user test fails.
 *
 * `getDashboard` is left pending in the present-user case so the component
 * stays on the loader and the loaded branch (data provider / canvas) never
 * mounts — keeping the mock surface minimal.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return { ...actual, useAuth: vi.fn() };
});

vi.mock("@/lib/dashboard/api", () => ({
  getDashboard: vi.fn(),
  saveDashboard: vi.fn(),
  getDefaultDashboard: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn(() => Promise.resolve([])) };
});

vi.mock("@/lib/hooks/use-is-mobile", () => ({
  useIsMobile: () => false,
}));

import CustomDashboard from "@/components/dashboard/CustomDashboard";
import { useAuth } from "@/components/auth/AuthProvider";
import { getDashboard } from "@/lib/dashboard/api";

describe("CustomDashboard — auth-readiness gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does NOT fetch the layout while user is null; shows the loader", () => {
    vi.mocked(useAuth).mockReturnValue({ user: null } as never);
    render(<CustomDashboard />);
    expect(screen.getByTestId("custom-dashboard-loading")).toBeInTheDocument();
    expect(getDashboard).not.toHaveBeenCalled();
  });

  it("fetches the layout exactly once user is present", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: { id: 1 } } as never);
    // Pending so the component stays on the loader (no loaded-branch mounts).
    vi.mocked(getDashboard).mockReturnValue(new Promise(() => {}) as never);
    render(<CustomDashboard />);
    await waitFor(() => expect(getDashboard).toHaveBeenCalledTimes(1));
  });
});
