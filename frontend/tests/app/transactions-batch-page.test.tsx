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

// CategorySelect is a heavyweight typeahead — stub it to a plain native
// <select> in tests so we can exercise the batch grid's row composition
// without dragging in the typeahead's dropdown lifecycle. The real
// component is covered by its own tests.
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

// DescriptionAutocomplete pulls a debounced /suggestions fetch on every
// keystroke. The grid-level tests below assert row composition,
// keyboard nav, and submit wiring, so stub it to a plain <input> that
// matches the prod component's external contract (value/onChange/
// ariaLabel). End-to-end autocomplete + pick + category-prefill is
// covered separately in `transactions-batch-page-autocomplete.test.tsx`.
vi.mock("@/components/transactions/DescriptionAutocomplete", () => ({
  default: ({
    id,
    value,
    onChange,
    ariaLabel,
    placeholder,
  }: {
    id: string;
    value: string;
    onChange: (next: string) => void;
    ariaLabel?: string;
    placeholder?: string;
  }) => (
    <input
      id={id}
      type="text"
      aria-label={ariaLabel}
      placeholder={placeholder}
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

async function fillRow(rowIndex: number) {
  const desc = await screen.findByLabelText(`Row ${rowIndex} description`);
  fireEvent.change(desc, { target: { value: `Coffee ${rowIndex}` } });

  const amount = screen.getByLabelText(`Row ${rowIndex} amount`);
  fireEvent.change(amount, { target: { value: "9.50" } });

  const account = screen.getByLabelText(`Row ${rowIndex} account`);
  fireEvent.change(account, { target: { value: "10" } });

  // Category select is stubbed to a native <select> in this test file.
  const cat = screen.getByLabelText(`Row ${rowIndex} category`);
  fireEvent.change(cat, { target: { value: "5" } });
}

describe("Batch entry page", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    defaultMock();
    setUser();
  });

  it("renders 5 default empty rows", async () => {
    render(<BatchEntryPage />);
    await waitFor(() => {
      expect(screen.getByLabelText("Row 1 description")).toBeTruthy();
      expect(screen.getByLabelText("Row 5 description")).toBeTruthy();
      expect(screen.queryByLabelText("Row 6 description")).toBeNull();
    });
  });

  it('appends a new row when "+ Add row" is clicked', async () => {
    render(<BatchEntryPage />);
    await screen.findByLabelText("Row 5 description");
    fireEvent.click(screen.getByRole("button", { name: /Add row/ }));
    await waitFor(() => {
      expect(screen.getByLabelText("Row 6 description")).toBeTruthy();
    });
  });

  it("removes a row when the trash button is clicked", async () => {
    render(<BatchEntryPage />);
    await screen.findByLabelText("Row 5 description");
    fireEvent.click(screen.getByLabelText("Remove row 3"));
    await waitFor(() => {
      // After removal, only 4 rows remain. The 5th label disappears.
      expect(screen.queryByLabelText("Row 5 description")).toBeNull();
    });
  });

  it("disables submit while no row is filled", async () => {
    render(<BatchEntryPage />);
    await screen.findByLabelText("Row 1 description");
    const submit = screen.getByRole("button", { name: /^Submit/ });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
  });

  it("submits filled rows and renders per-row outcomes (happy path)", async () => {
    render(<BatchEntryPage />);
    await screen.findByLabelText("Row 1 description");

    await fillRow(1);

    vi.mocked(apiFetch).mockImplementationOnce(() =>
      Promise.resolve({
        imported_count: 1,
        error_count: 0,
        results: [{ row_number: 1, transaction_id: 42 }],
        errors: [],
      }),
    );

    const submit = screen.getByRole("button", { name: /Submit 1 row$/ });
    expect((submit as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => {
      expect(screen.getByLabelText("Row 1 imported")).toBeTruthy();
    });
    expect(screen.getByRole("status").textContent).toMatch(/1 row imported/);
  });

  it("renders per-row error message on partial-success response", async () => {
    render(<BatchEntryPage />);
    await screen.findByLabelText("Row 1 description");

    await fillRow(1);

    vi.mocked(apiFetch).mockImplementationOnce(() =>
      Promise.resolve({
        imported_count: 0,
        error_count: 1,
        results: [],
        errors: [{ row_number: 1, error: "Invalid amount" }],
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Submit 1 row$/ }));

    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((n) => n.textContent?.includes("Invalid amount"))).toBe(true);
    });
  });

  it("adds a new row on Enter at the last cell of the last row", async () => {
    render(<BatchEntryPage />);
    await screen.findByLabelText("Row 5 description");
    // The trash button on the last row is the final focusable cell.
    const removeLast = screen.getByLabelText("Remove row 5");
    fireEvent.keyDown(removeLast, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByLabelText("Row 6 description")).toBeTruthy();
    });
  });
});
