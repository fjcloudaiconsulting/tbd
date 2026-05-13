import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import ReconcileClient from "@/app/import/[import_id]/reconcile/ReconcileClient";
import { apiFetch } from "@/lib/api";
import type { ImportBatchDetail } from "@/lib/types";

// Fresh SWR cache per test prevents fallback bleed across `it()`
// blocks. Without this, the second test's GET request can return the
// first test's cached batch.
function renderClient(
  props: React.ComponentProps<typeof ReconcileClient>,
) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <ReconcileClient {...props} />
    </SWRConfig>,
  );
}

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/HelpAnchor", () => ({
  default: () => <span data-testid="help-anchor" />,
}));

vi.mock("@/components/ui/Spinner", () => ({
  default: () => <span data-testid="spinner" />,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});

function makeBatch(overrides: Partial<ImportBatchDetail> = {}): ImportBatchDetail {
  return {
    batch: {
      id: 7,
      account_id: 1,
      source_format: "csv",
      file_name: "ing-2026-05.csv",
      created_at: "2026-05-13T08:00:00Z",
      created_by_user_id: 1,
      status: "open",
      total_rows: 3,
      pending_count: 2,
    },
    rows: [
      {
        transaction_id: 100,
        date: "2026-05-10",
        description: "Albert Heijn",
        amount: "12.50",
        type: "expense",
        reconciliation_state: "pending_review",
        fitid: "FITID-1",
        linked_transaction_id: null,
        duplicate_warning: false,
        duplicate_warning_target: null,
      },
      {
        transaction_id: 101,
        date: "2026-05-11",
        description: "Salary",
        amount: "2500.00",
        type: "income",
        reconciliation_state: "accepted",
        fitid: null,
        linked_transaction_id: null,
        duplicate_warning: false,
        duplicate_warning_target: null,
      },
      {
        transaction_id: 102,
        date: "2026-05-12",
        description: "Gas station",
        amount: "45.00",
        type: "expense",
        reconciliation_state: "pending_review",
        fitid: "FITID-2",
        linked_transaction_id: null,
        duplicate_warning: true,
        duplicate_warning_target: 999,
      },
    ],
    ...overrides,
  };
}

describe("ReconcileClient", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(
      async (path: string) => makeBatch(),
    );
  });

  it("renders the batch header with file name and progress", async () => {
    renderClient({ batchId: 7, initialBatch: makeBatch() });
    expect(await screen.findByText("CSV import")).toBeInTheDocument();
    expect(screen.getByText("ing-2026-05.csv")).toBeInTheDocument();
    expect(screen.getByText(/1 of 3/)).toBeInTheDocument();
  });

  it("renders one row card per transaction", async () => {
    renderClient({ batchId: 7, initialBatch: makeBatch() });
    const rows = await screen.findAllByTestId("reconcile-row");
    expect(rows).toHaveLength(3);
  });

  it("shows the duplicate warning callout for rows flagged by the server", async () => {
    renderClient({ batchId: 7, initialBatch: makeBatch() });
    const warning = await screen.findByTestId("duplicate-warning");
    expect(warning).toHaveTextContent(/duplicate/i);
    expect(warning).toHaveTextContent("#999");
  });

  it("offers Accept / Skip / Reject buttons on a pending row", async () => {
    renderClient({ batchId: 7, initialBatch: makeBatch() });
    const rows = await screen.findAllByTestId("reconcile-row");
    const pendingRow = rows[0];
    expect(
      pendingRow.querySelector('[data-testid="action-accepted"]'),
    ).toBeTruthy();
    expect(
      pendingRow.querySelector('[data-testid="action-skipped"]'),
    ).toBeTruthy();
    expect(
      pendingRow.querySelector('[data-testid="action-rejected"]'),
    ).toBeTruthy();
  });

  it("fires the reconcile POST when Accept is clicked", async () => {
    const reconcileMock = vi.fn().mockResolvedValue({
      import_id: 7,
      transitioned: [100],
      errors: [],
      remaining_pending: 1,
      batch_status: "open",
    });
    const getMock = vi.fn().mockResolvedValue(makeBatch());
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(
      async (path: string, options?: RequestInit) => {
        if (options?.method === "POST") return reconcileMock(path, options);
        return getMock(path);
      },
    );

    renderClient({ batchId: 7, initialBatch: makeBatch() });
    const rows = await screen.findAllByTestId("reconcile-row");
    const pendingRow = rows[0];
    const acceptBtn = pendingRow.querySelector(
      '[data-testid="action-accepted"]',
    ) as HTMLButtonElement;
    fireEvent.click(acceptBtn);

    await waitFor(() => {
      expect(reconcileMock).toHaveBeenCalledWith(
        "/api/v1/import/7/reconcile",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const call = reconcileMock.mock.calls[0];
    const body = JSON.parse(call[1].body);
    expect(body.transitions).toEqual([
      { transaction_id: 100, to_state: "accepted" },
    ]);
  });

  it("renders the closed-batch badge when the server reports status=closed", async () => {
    const closed = makeBatch({
      batch: {
        ...makeBatch().batch,
        status: "closed",
        pending_count: 0,
      },
    });
    renderClient({ batchId: 7, initialBatch: closed });
    expect(await screen.findByText(/batch closed/i)).toBeInTheDocument();
  });

  it("renders the empty state when the batch has no rows", async () => {
    const empty = makeBatch({
      rows: [],
      batch: { ...makeBatch().batch, total_rows: 0, pending_count: 0 },
    });
    // Override the default mock so the SWR fetcher returns the empty
    // batch too -- otherwise the GET revalidate clobbers our fallback.
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(
      async () => empty,
    );
    renderClient({ batchId: 7, initialBatch: empty });
    expect(
      await screen.findByText(/no rows to reconcile/i),
    ).toBeInTheDocument();
  });

  it("renders the error state when the initial fetch returned null", async () => {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(
      async () => {
        throw new Error("404");
      },
    );
    renderClient({ batchId: 7, initialBatch: null });
    await waitFor(() => {
      expect(
        screen.getByText(/could not load this import batch/i),
      ).toBeInTheDocument();
    });
  });
});
