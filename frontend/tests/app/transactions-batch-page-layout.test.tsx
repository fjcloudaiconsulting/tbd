/**
 * Regression coverage for the 2026-05-13 layout bug.
 *
 * Symptom: column header labels (`<th>` with `${label}` token) inherited
 * `display: block` + `mb-1.5` from `lib/styles.ts`. The `block` override
 * forced every header out of `display: table-cell`, stacking them
 * vertically while `<tbody>`'s `<td>`s rendered as horizontal table
 * cells. Users saw a left-aligned vertical list (DATE / DESCRIPTION /
 * AMOUNT / ...) above a single row of inputs that didn't match the
 * stacked headers in count or column width.
 *
 * Fix in `app/transactions/batch/page.tsx`:
 *   - `<th>` no longer applies the `${label}` token; uses a local
 *     `thLabel` class that drops `block` + `mb-1.5`.
 *   - Row template now matches headers 1:1 (9 columns):
 *     [#] [Date] [Description] [Amount] [Type] [Account] [Category]
 *     [Status] [Result], plus a trailing `<th>` for the delete button.
 *   - Status column = transaction status (settled / pending) select,
 *     matching the single-transaction form. Submission outcome moved
 *     to a separate `Result` column.
 *
 * This file asserts:
 *   1. Header order matches the row-cell order (the layout-bug
 *      regression test).
 *   2. Every row exposes all expected inputs (incl. Description
 *      autocomplete + Status select).
 *   3. The Status select submits as `status: "settled" | "pending"`
 *      on the batch payload, wiring the new field end-to-end.
 *   4. A snapshot of the `<thead>` row (column order + classes) so
 *      future refactors fail loudly if the header structure drifts.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import BatchEntryPage from "@/app/transactions/batch/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

// Stub CategorySelect to a native <select> like the sibling tests do,
// so we exercise grid layout without dragging in the typeahead.
vi.mock("@/components/ui/CategorySelect", () => ({
  default: ({
    value,
    onChange,
    categories,
    "aria-label": ariaLabel,
  }: {
    value: number | "";
    onChange: (id: number | "") => void;
    categories: { id: number; name: string }[];
    "aria-label"?: string;
  }) => (
    <select
      aria-label={ariaLabel}
      value={value === "" ? "" : String(value)}
      onChange={(e) =>
        onChange(e.target.value === "" ? "" : Number(e.target.value))
      }
    >
      <option value="">Pick…</option>
      {categories.map((c) => (
        <option key={c.id} value={c.id}>
          {c.name}
        </option>
      ))}
    </select>
  ),
}));

vi.mock("@/components/transactions/DescriptionAutocomplete", () => ({
  default: ({
    id,
    value,
    onChange,
    ariaLabel,
  }: {
    id: string;
    value: string;
    onChange: (next: string) => void;
    ariaLabel?: string;
  }) => (
    <input
      id={id}
      type="text"
      aria-label={ariaLabel}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/transactions/batch",
}));

const ACCOUNTS = [
  {
    id: 10,
    name: "Primary",
    account_type_id: 1,
    account_type_name: "Checking",
    account_type_slug: "checking",
    balance: "150.00",
    currency: "EUR",
    is_active: true,
    is_default: true,
    close_day: null,
  },
];

const CATEGORIES = [
  {
    id: 5,
    name: "Groceries",
    slug: "groceries",
    parent_id: null,
    type: "expense",
    is_system: false,
  },
];

const BASE_USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  password_set: true,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

function defaultMock() {
  vi.mocked(apiFetch).mockImplementation((path: string) => {
    if (path === "/api/v1/accounts") return Promise.resolve(ACCOUNTS);
    if (path === "/api/v1/categories") return Promise.resolve(CATEGORIES);
    return Promise.resolve([]);
  });
}

function setUser() {
  vi.mocked(useAuth).mockReturnValue({
    user: { ...BASE_USER, role: "owner" } as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

describe("Batch entry layout (regression 2026-05-13)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    defaultMock();
    setUser();
  });

  it("renders the headers in the same order as the row cells", async () => {
    const { container } = render(<BatchEntryPage />);
    await screen.findByLabelText("Row 1 description");

    const headerCells = Array.from(container.querySelectorAll("thead th"));
    const headerLabels = headerCells
      .map((th) => th.textContent?.trim())
      .filter((s): s is string => Boolean(s) && s !== "Remove");

    expect(headerLabels).toEqual([
      "#",
      "Date",
      "Description",
      "Amount",
      "Type",
      "Account",
      "Category",
      "Status",
      "Result",
    ]);

    // First-row cell order must align with the header order via data-label.
    const firstRow = container.querySelector("tbody tr");
    expect(firstRow).not.toBeNull();
    const cellLabels = Array.from(firstRow!.querySelectorAll("td[data-label]"))
      .map((td) => td.getAttribute("data-label"));
    expect(cellLabels).toEqual([
      "Row",
      "Date",
      "Description",
      "Amount",
      "Type",
      "Account",
      "Category",
      "Status",
      "Result",
    ]);
  });

  it("does not apply `display: block` to header cells", async () => {
    const { container } = render(<BatchEntryPage />);
    await screen.findByLabelText("Row 1 description");

    // The bug was caused by the shared `label` style (which includes
    // `block`) being applied to `<th>` elements. Guard against that
    // regression by asserting no header cell carries the token's
    // signature classes (`mb-1.5` + `block`) together.
    const headerCells = Array.from(container.querySelectorAll("thead th"));
    for (const th of headerCells) {
      const cls = th.className;
      const hasBlock = cls.split(/\s+/).includes("block");
      expect(hasBlock).toBe(false);
    }
  });

  it("renders every input cell per row (incl. description + status)", async () => {
    render(<BatchEntryPage />);
    // Description was missing from the broken visible row layout.
    expect(await screen.findByLabelText("Row 1 description")).toBeTruthy();
    expect(screen.getByLabelText("Row 1 date")).toBeTruthy();
    expect(screen.getByLabelText("Row 1 amount")).toBeTruthy();
    expect(screen.getByLabelText("Row 1 type")).toBeTruthy();
    expect(screen.getByLabelText("Row 1 account")).toBeTruthy();
    expect(screen.getByLabelText("Row 1 category")).toBeTruthy();
    // Status select was missing entirely before the fix.
    const statusSel = screen.getByLabelText("Row 1 status") as HTMLSelectElement;
    expect(statusSel.tagName).toBe("SELECT");
    const options = Array.from(statusSel.options).map((o) => o.value);
    expect(options).toEqual(["settled", "pending"]);
    expect(statusSel.value).toBe("settled");
  });

  it("sends row.status on the batch payload", async () => {
    render(<BatchEntryPage />);
    await screen.findByLabelText("Row 1 description");

    fireEvent.change(screen.getByLabelText("Row 1 description"), {
      target: { value: "Coffee" },
    });
    fireEvent.change(screen.getByLabelText("Row 1 amount"), {
      target: { value: "9.50" },
    });
    fireEvent.change(screen.getByLabelText("Row 1 account"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("Row 1 category"), {
      target: { value: "5" },
    });
    fireEvent.change(screen.getByLabelText("Row 1 status"), {
      target: { value: "pending" },
    });

    let capturedBody: string | null = null;
    vi.mocked(apiFetch).mockImplementationOnce((path, init) => {
      capturedBody = (init?.body as string) ?? null;
      return Promise.resolve({
        imported_count: 1,
        error_count: 0,
        results: [{ row_number: 1, transaction_id: 99 }],
        errors: [],
      });
    });

    fireEvent.click(screen.getByRole("button", { name: /Submit 1 row$/ }));

    await waitFor(() => {
      expect(capturedBody).not.toBeNull();
    });
    const parsed = JSON.parse(capturedBody as unknown as string);
    expect(parsed.rows[0].transaction.status).toBe("pending");
  });

  it("matches the canonical thead snapshot", async () => {
    const { container } = render(<BatchEntryPage />);
    await screen.findByLabelText("Row 1 description");
    const thead = container.querySelector(".batch-grid > thead");
    expect(thead).toMatchInlineSnapshot(`
      <thead
        class="batch-grid__head"
      >
        <tr
          class="border-b border-border text-left"
        >
          <th
            class="w-10 px-2 py-2 text-xs text-text-muted"
            scope="col"
          >
            #
          </th>
          <th
            class="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted w-32 px-2 py-2"
            scope="col"
          >
            Date
          </th>
          <th
            class="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted w-64 px-2 py-2"
            scope="col"
          >
            Description
          </th>
          <th
            class="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted w-28 px-2 py-2"
            scope="col"
          >
            Amount
          </th>
          <th
            class="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted w-28 px-2 py-2"
            scope="col"
          >
            Type
          </th>
          <th
            class="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted w-44 px-2 py-2"
            scope="col"
          >
            Account
          </th>
          <th
            class="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted w-44 px-2 py-2"
            scope="col"
          >
            Category
          </th>
          <th
            class="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted w-32 px-2 py-2"
            scope="col"
          >
            Status
          </th>
          <th
            class="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted w-32 px-2 py-2"
            scope="col"
          >
            Result
          </th>
          <th
            class="w-10 px-2 py-2"
            scope="col"
          >
            <span
              class="sr-only"
            >
              Remove
            </span>
          </th>
        </tr>
      </thead>
    `);
  });
});
