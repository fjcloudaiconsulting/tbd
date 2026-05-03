import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ImportPage from "@/app/import/page";
import { apiFetch } from "@/lib/api";
import type { ImportPreviewResponse, ImportPreviewRow } from "@/lib/types";

// Mock Next.js navigation hooks. The page uses both useRouter and
// useSearchParams; the latter must implement .get().
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => ({ get: () => null }),
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const ACCOUNT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Bank",
  account_type_slug: "bank",
  balance: 100,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const CATEGORY_EXP = {
  id: 5,
  name: "Groceries",
  type: "expense" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "groceries",
  is_system: false,
  transaction_count: 0,
};

const CATEGORY_INC = {
  id: 6,
  name: "Salary",
  type: "income" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "salary",
  is_system: false,
  transaction_count: 0,
};

function baseRow(overrides: Partial<ImportPreviewRow> = {}): ImportPreviewRow {
  return {
    row_number: 1,
    date: "2026-05-01",
    description: "Test row",
    amount: 50,
    type: "expense",
    counterparty: null,
    transaction_type: null,
    is_duplicate: false,
    duplicate_transaction_id: null,
    suggested_category_id: null,
    suggestion_source: null,
    is_duplicate_of_linked_leg: false,
    duplicate_candidate: null,
    default_action_drop: false,
    transfer_match_action: "none",
    transfer_match_confidence: null,
    pair_with_transaction_id: null,
    transfer_candidates: [],
    ...overrides,
  };
}

function basePreview(rows: ImportPreviewRow[]): ImportPreviewResponse {
  return {
    rows,
    account_id: 1,
    file_name: "test.csv",
    total_rows: rows.length,
    duplicate_count: 0,
    auto_paired_count: rows.filter((r) => r.transfer_match_action === "pair_with").length,
    suggested_pair_count: rows.filter((r) => r.transfer_match_action === "suggest_pair").length,
    multi_candidate_count: rows.filter((r) => r.transfer_match_action === "choose_candidate").length,
    duplicate_of_linked_count: rows.filter((r) => r.is_duplicate_of_linked_leg).length,
  };
}

/**
 * Render the page, wait for accounts/categories to load (so the upload form
 * shows), then trigger the file upload to drop the preview into state.
 */
async function renderAndPreview(preview: ImportPreviewResponse) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([ACCOUNT]);
    if (url === "/api/v1/categories") return Promise.resolve([CATEGORY_EXP, CATEGORY_INC]);
    if (url === "/api/v1/import/preview") return Promise.resolve(preview);
    return Promise.resolve(undefined);
  }) as never);

  render(<ImportPage />);

  // Upload step renders once the categories array is populated.
  const uploadButton = await screen.findByRole("button", { name: /upload & preview/i });

  // Drop a fake CSV file onto the file input.
  const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["date,desc,amt\n"], "test.csv", { type: "text/csv" });
  fireEvent.change(fileInput, { target: { files: [file] } });

  fireEvent.click(uploadButton);

  // Wait for the preview table to render.
  await screen.findByText("test.csv");
}

describe("ImportPage transfer pill column", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("renders Pair as transfer pill on same-day match", async () => {
    const preview = basePreview([
      baseRow({
        row_number: 1,
        transfer_match_action: "pair_with",
        transfer_match_confidence: "same_day",
        pair_with_transaction_id: 99,
        transfer_candidates: [
          {
            id: 99,
            date: "2026-05-01",
            description: "Counter leg",
            amount: 50,
            account_id: 2,
            account_name: "Savings",
            date_diff_days: 0,
            confidence: "same_day",
          },
        ],
      }),
    ]);

    await renderAndPreview(preview);

    // Pill text visible.
    const pill = await screen.findByTestId("transfer-pill-1");
    expect(pill).toHaveTextContent("Pair as transfer");

    // Click pill to open panel and assert checkbox is pre-checked.
    fireEvent.click(pill);
    const panel = await screen.findByTestId("transfer-panel-1");
    const checkbox = panel.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox).not.toBeNull();
    expect(checkbox.checked).toBe(true);
  });

  it("renders Possible transfer pill on near-date match without preselect", async () => {
    const preview = basePreview([
      baseRow({
        row_number: 1,
        transfer_match_action: "suggest_pair",
        transfer_match_confidence: "near_date",
        pair_with_transaction_id: 99,
        transfer_candidates: [
          {
            id: 99,
            date: "2026-04-29",
            description: "Counter leg near",
            amount: 50,
            account_id: 2,
            account_name: "Savings",
            date_diff_days: 2,
            confidence: "near_date",
          },
        ],
      }),
    ]);

    await renderAndPreview(preview);

    const pill = await screen.findByTestId("transfer-pill-1");
    expect(pill.textContent).toMatch(/Possible transfer/);
    expect(pill.textContent).toMatch(/±2 days/);

    fireEvent.click(pill);
    const panel = await screen.findByTestId("transfer-panel-1");
    const checkbox = panel.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox).not.toBeNull();
    expect(checkbox.checked).toBe(false);
  });

  it("renders Multiple candidates pill that opens chooser", async () => {
    const preview = basePreview([
      baseRow({
        row_number: 1,
        transfer_match_action: "choose_candidate",
        transfer_match_confidence: "multi_candidate",
        pair_with_transaction_id: null,
        transfer_candidates: [
          {
            id: 101,
            date: "2026-05-01",
            description: "First candidate",
            amount: 50,
            account_id: 2,
            account_name: "Savings",
            date_diff_days: 0,
            confidence: "same_day",
          },
          {
            id: 102,
            date: "2026-04-30",
            description: "Second candidate",
            amount: 50,
            account_id: 3,
            account_name: "Brokerage",
            date_diff_days: 1,
            confidence: "near_date",
          },
        ],
      }),
    ]);

    await renderAndPreview(preview);

    const pill = await screen.findByTestId("transfer-pill-1");
    expect(pill).toHaveTextContent("Multiple candidates");

    // Click pill to open chooser panel.
    fireEvent.click(pill);
    const panel = await screen.findByTestId("transfer-panel-1");

    // Both candidates plus the "Skip — don't pair" radio.
    const radios = panel.querySelectorAll('input[type="radio"]');
    expect(radios.length).toBe(3);

    expect(panel.textContent).toContain("First candidate");
    expect(panel.textContent).toContain("Second candidate");

    // Closest hint is rendered next to candidate 0.
    expect(panel.textContent).toContain("closest");
  });

  it("renders Drop as duplicate pill with synthetic-leg badge when existing_leg_is_imported is false", async () => {
    const preview = basePreview([
      baseRow({
        row_number: 1,
        is_duplicate_of_linked_leg: true,
        default_action_drop: true,
        duplicate_candidate: {
          id: 77,
          date: "2026-05-01",
          description: "Existing linked leg",
          amount: 50,
          account_id: 1,
          account_name: "Checking",
          existing_leg_is_imported: false,
        },
      }),
    ]);

    await renderAndPreview(preview);

    const pill = await screen.findByTestId("transfer-pill-1");
    expect(pill).toHaveTextContent("Drop as duplicate");

    fireEvent.click(pill);
    const panel = await screen.findByTestId("transfer-panel-1");
    expect(panel.textContent).toContain("Synthetic leg from convert-to-transfer");

    // Default Drop checkbox is pre-checked.
    const checkbox = panel.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox).not.toBeNull();
    expect(checkbox.checked).toBe(true);
  });

  it("Review pairings filter shows only rows with transfer_match_action != none or duplicate", async () => {
    const preview = basePreview([
      baseRow({
        row_number: 1,
        description: "Plain row",
        // transfer_match_action defaults to "none".
      }),
      baseRow({
        row_number: 2,
        description: "Pair row",
        transfer_match_action: "pair_with",
        transfer_match_confidence: "same_day",
        pair_with_transaction_id: 99,
        transfer_candidates: [
          {
            id: 99,
            date: "2026-05-01",
            description: "Counter leg",
            amount: 50,
            account_id: 2,
            account_name: "Savings",
            date_diff_days: 0,
            confidence: "same_day",
          },
        ],
      }),
      baseRow({
        row_number: 3,
        description: "Duplicate-of-linked row",
        is_duplicate_of_linked_leg: true,
        default_action_drop: true,
        duplicate_candidate: {
          id: 77,
          date: "2026-05-01",
          description: "Existing leg",
          amount: 50,
          account_id: 1,
          account_name: "Checking",
          existing_leg_is_imported: true,
        },
      }),
    ]);

    await renderAndPreview(preview);

    // Initially all 3 rows visible.
    expect(screen.getByText("Plain row")).toBeInTheDocument();
    expect(screen.getByText("Pair row")).toBeInTheDocument();
    expect(screen.getByText("Duplicate-of-linked row")).toBeInTheDocument();

    // Toggle filter on.
    const toggle = screen.getByTestId("review-pairings-toggle") as HTMLInputElement;
    fireEvent.click(toggle);
    expect(toggle.checked).toBe(true);

    // The "none" row should be hidden; pair + duplicate-of-linked stay.
    expect(screen.queryByText("Plain row")).toBeNull();
    expect(screen.getByText("Pair row")).toBeInTheDocument();
    expect(screen.getByText("Duplicate-of-linked row")).toBeInTheDocument();
  });

  it("Confirm payload sets action correctly per row state", async () => {
    const preview = basePreview([
      baseRow({
        row_number: 1,
        description: "Same-day pair",
        transfer_match_action: "pair_with",
        transfer_match_confidence: "same_day",
        pair_with_transaction_id: 201,
        transfer_candidates: [
          {
            id: 201,
            date: "2026-05-01",
            description: "Counter A",
            amount: 50,
            account_id: 2,
            account_name: "Savings",
            date_diff_days: 0,
            confidence: "same_day",
          },
        ],
      }),
      baseRow({
        row_number: 2,
        description: "Suggest pair",
        transfer_match_action: "suggest_pair",
        transfer_match_confidence: "near_date",
        pair_with_transaction_id: 202,
        transfer_candidates: [
          {
            id: 202,
            date: "2026-04-29",
            description: "Counter B",
            amount: 50,
            account_id: 2,
            account_name: "Savings",
            date_diff_days: 2,
            confidence: "near_date",
          },
        ],
      }),
      baseRow({
        row_number: 3,
        description: "Drop linked",
        is_duplicate_of_linked_leg: true,
        default_action_drop: true,
        duplicate_candidate: {
          id: 303,
          date: "2026-05-01",
          description: "Existing leg",
          amount: 50,
          account_id: 1,
          account_name: "Checking",
          existing_leg_is_imported: true,
        },
      }),
    ]);

    await renderAndPreview(preview);

    // The suggest_pair row's checkbox starts unchecked. Open panel and accept.
    const suggestPill = await screen.findByTestId("transfer-pill-2");
    fireEvent.click(suggestPill);
    const suggestPanel = await screen.findByTestId("transfer-panel-2");
    const suggestCheckbox = suggestPanel.querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement;
    fireEvent.click(suggestCheckbox);
    expect(suggestCheckbox.checked).toBe(true);

    // Provide a default category so confirm is enabled. The page renders only
    // the "Default Category" native <select> on the preview step; per-row
    // category pickers use a custom CategorySelect (not a native select).
    const selects = document.querySelectorAll("select");
    const defaultCatSelect = selects[selects.length - 1] as HTMLSelectElement;
    fireEvent.change(defaultCatSelect, { target: { value: "5" } });

    // Stub the confirm response so the click resolves cleanly.
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockResolvedValueOnce({
      imported_count: 2,
      paired_count: 0,
      dropped_duplicate_count: 0,
      skipped_count: 1,
      error_count: 0,
      errors: [],
    } as never);

    const confirmBtn = screen.getByRole("button", { name: /import 3 transactions/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      const confirmCall = apiFetchMock.mock.calls.find(
        ([url]) => url === "/api/v1/import/confirm",
      );
      expect(confirmCall).toBeDefined();
    });

    const confirmCall = apiFetchMock.mock.calls.find(
      ([url]) => url === "/api/v1/import/confirm",
    )!;
    const body = JSON.parse((confirmCall[1] as RequestInit).body as string);

    expect(body.rows).toHaveLength(3);
    expect(body.rows[0].action).toBe("pair_with_existing");
    expect(body.rows[0].pair_with_transaction_id).toBe(201);
    expect(body.rows[0].duplicate_of_transaction_id).toBeNull();

    expect(body.rows[1].action).toBe("pair_with_existing");
    expect(body.rows[1].pair_with_transaction_id).toBe(202);

    expect(body.rows[2].action).toBe("drop_as_duplicate");
    expect(body.rows[2].duplicate_of_transaction_id).toBe(303);
    expect(body.rows[2].pair_with_transaction_id).toBeNull();
  });

  it("hides the Transfer column header when no row has any transfer state", async () => {
    // All rows are plain (no detector hits, no duplicate-of-linked).
    const preview = basePreview([
      baseRow({ row_number: 1, description: "Plain row 1" }),
      baseRow({ row_number: 2, description: "Plain row 2" }),
    ]);

    await renderAndPreview(preview);

    // Body is rendered.
    expect(screen.getByText("Plain row 1")).toBeInTheDocument();

    // Transfer header should NOT be present when there is no transfer state at all.
    const transferHeader = screen.queryByRole("columnheader", { name: /^Transfer$/i });
    expect(transferHeader).toBeNull();
  });

  it("shows the Transfer column header when at least one row has transfer state", async () => {
    const preview = basePreview([
      baseRow({ row_number: 1, description: "Plain row" }),
      baseRow({
        row_number: 2,
        description: "Pair row",
        transfer_match_action: "pair_with",
        transfer_match_confidence: "same_day",
        pair_with_transaction_id: 99,
        transfer_candidates: [
          {
            id: 99,
            date: "2026-05-01",
            description: "Counter leg",
            amount: 50,
            account_id: 2,
            account_name: "Savings",
            date_diff_days: 0,
            confidence: "same_day",
          },
        ],
      }),
    ]);

    await renderAndPreview(preview);

    // Transfer header is present.
    const transferHeader = screen.getByRole("columnheader", { name: /^Transfer$/i });
    expect(transferHeader).toBeInTheDocument();

    // Pill is rendered for the matched row.
    expect(screen.getByTestId("transfer-pill-2")).toBeInTheDocument();
  });

  it("results page surfaces paired_count and dropped_duplicate_count", async () => {
    const preview = basePreview([baseRow({ row_number: 1 })]);

    await renderAndPreview(preview);

    // Provide a default category so the confirm button is enabled.
    const selects = document.querySelectorAll("select");
    const defaultCatSelect = selects[selects.length - 1] as HTMLSelectElement;
    fireEvent.change(defaultCatSelect, { target: { value: "5" } });

    // Stub the confirm response with non-zero paired/dropped counters.
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockResolvedValueOnce({
      imported_count: 3,
      paired_count: 2,
      dropped_duplicate_count: 1,
      skipped_count: 0,
      error_count: 0,
      errors: [],
    } as never);

    const confirmBtn = screen.getByRole("button", { name: /import 1 transaction/i });
    fireEvent.click(confirmBtn);

    // Results step renders the new counter rows.
    await screen.findByText(/import complete/i);
    expect(screen.getByText(/2 paired as transfers/i)).toBeInTheDocument();
    expect(
      screen.getByText(/1 dropped as duplicate of existing transfer leg/i),
    ).toBeInTheDocument();
  });
});
