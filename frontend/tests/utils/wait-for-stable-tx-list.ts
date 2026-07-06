import { screen, waitFor } from "@testing-library/react";

import { apiFetch } from "@/lib/api";

/**
 * The transactions page now issues a single GET /api/v1/transactions at
 * mount: the initial list fetch waits for the billing-periods SWR request
 * to settle first (the cold-mount single-fetch guard), so loadTransactions
 * no longer re-fires when `periods` resolves. That removes the old flicker
 * where the table briefly reverted to a Spinner and dropped a just-set
 * editingId between an Edit click and the re-render.
 *
 * This helper waits for that one GET /api/v1/transactions call to have
 * happened AND for the Edit buttons to be present, so subsequent clicks
 * aren't clobbered.
 *
 * Callers must have `vi.mock("@/lib/api", ...)` in scope so that the
 * imported `apiFetch` here is the same mocked function the test wires up.
 */
export async function waitForStableTxList(): Promise<void> {
  const apiFetchMock = vi.mocked(apiFetch);
  await waitFor(() => {
    const txGetCalls = apiFetchMock.mock.calls.filter(
      (c) =>
        typeof c[0] === "string" &&
        (c[0] as string).startsWith("/api/v1/transactions") &&
        ((c[1] as RequestInit | undefined)?.method ?? "GET") === "GET",
    );
    expect(txGetCalls.length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole("status", { name: /loading/i })).toBeNull();
    expect(
      screen.queryAllByRole("button", { name: /^Edit:/ }).length,
    ).toBeGreaterThan(0);
  });
}
