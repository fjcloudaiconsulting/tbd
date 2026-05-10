import { screen, waitFor } from "@testing-library/react";

import { apiFetch } from "@/lib/api";

/**
 * The transactions page issues two fetches at mount: loadRefs (which
 * updates `periods`) and loadTransactions. Because loadTransactions
 * depends on `periods`, the second setPeriods call (even to the same
 * empty array) re-triggers loadTransactions, briefly flipping the
 * `fetching` flag back to true and replacing the table with a Spinner.
 * Tests that race past the spinner can land between Edit-button clicks
 * and the post-spinner re-render, dropping the just-set editingId.
 *
 * This helper waits for the GET /api/v1/transactions call to have
 * happened at least twice (initial + post-loadRefs re-fetch) AND for
 * the Edit buttons to be present, so subsequent clicks aren't clobbered.
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
    expect(txGetCalls.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByRole("status", { name: /loading/i })).toBeNull();
    expect(
      screen.queryAllByRole("button", { name: /^Edit:/ }).length,
    ).toBeGreaterThan(0);
  });
}
