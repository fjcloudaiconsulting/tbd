/**
 * Batch entry + description autocomplete integration tests.
 *
 * Verifies the wiring between the multi-row batch grid
 * (`/transactions/batch`) and the reusable `DescriptionAutocomplete`
 * component:
 *
 *  1. Each row's autocomplete has an isolated fetch lifecycle. Typing
 *     in row 1 fires one fetch tagged with row 1's value; row 2's
 *     state is untouched.
 *  2. Picking a suggestion in row 1 populates row 1's description AND
 *     fills row 1's category (when the row's category is still empty),
 *     without touching row 2.
 *  3. Tab from row 1's description moves to the next CELL of row 1,
 *     not to row 2 (the autocomplete's combobox doesn't trap Tab).
 *  4. Submitting a batch where rows were populated via the autocomplete
 *     persists those rows through `/api/v1/transactions/batch` with
 *     the correct description + category_id payloads.
 *
 * We replace `apiFetch` with a single mock that branches on URL so the
 * autocomplete's real fetch path (300ms debounce + AbortController)
 * runs unmocked. `vi.useFakeTimers()` lets us step past the debounce
 * without sleeping.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import BatchEntryPage from "@/app/transactions/batch/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => (
      <>{children}</>
    ),
  };
});

// CategorySelect stubbed to a native select so we can read/set
// `Row N category` like the sibling test file does.
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
  {
    id: 6,
    name: "Coffee",
    slug: "coffee",
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

const SUGGESTION_ALBERT = {
  description: "Albert Heijn",
  category_id: 5,
  category_name: "Groceries",
  use_count: 12,
  last_used: "2026-05-10",
};

/** Records every /suggestions request the autocomplete dispatches so
 *  tests can assert per-row isolation. */
type RecordedCall = {
  url: string;
  q: string;
  type: string;
};

function makeApiMock(recorded: RecordedCall[]) {
  vi.mocked(apiFetch).mockImplementation((path: string) => {
    if (path === "/api/v1/accounts") return Promise.resolve(ACCOUNTS);
    if (path === "/api/v1/categories") return Promise.resolve(CATEGORIES);
    if (path.startsWith("/api/v1/transactions/suggestions/descriptions")) {
      const url = new URL(path, "http://localhost");
      recorded.push({
        url: path,
        q: url.searchParams.get("q") ?? "",
        type: url.searchParams.get("type") ?? "",
      });
      // Match suggestions when q starts with "Albert".
      if ((url.searchParams.get("q") ?? "").startsWith("Albert")) {
        return Promise.resolve({ suggestions: [SUGGESTION_ALBERT] });
      }
      return Promise.resolve({ suggestions: [] });
    }
    if (path === "/api/v1/transactions/batch") {
      return Promise.resolve({
        imported_count: 1,
        error_count: 0,
        results: [{ row_number: 1, transaction_id: 99 }],
        errors: [],
      });
    }
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

/** Step past the 300ms autocomplete debounce + resolve the pending
 *  microtask queue so the fetcher's `.then()` lands. */
async function flushDebounce() {
  await act(async () => {
    vi.advanceTimersByTime(350);
  });
  // Two microtask drains — one for the fetch promise, one for the
  // setState batch the component schedules from inside the .then().
  await act(async () => {
    await Promise.resolve();
  });
  await act(async () => {
    await Promise.resolve();
  });
}

describe("Batch entry — description autocomplete integration", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(apiFetch).mockReset();
    setUser();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("isolates per-row autocomplete fetches", async () => {
    const recorded: RecordedCall[] = [];
    makeApiMock(recorded);
    render(<BatchEntryPage />);

    // The initial /accounts + /categories awaits land on real timers'
    // microtasks, so advance until the description inputs exist.
    await waitFor(() => screen.getByLabelText("Row 1 description"));

    // Type in row 1 only.
    const row1Desc = screen.getByLabelText("Row 1 description");
    fireEvent.change(row1Desc, { target: { value: "Albert" } });

    await flushDebounce();

    // Exactly one suggestions call, tagged with row 1's value.
    const suggestionCalls = recorded.filter((c) => c.q === "Albert");
    expect(suggestionCalls).toHaveLength(1);
    expect(suggestionCalls[0].type).toBe("expense");

    // Row 2 input is still empty — no fetch was issued for it.
    const row2Desc = screen.getByLabelText("Row 2 description") as HTMLInputElement;
    expect(row2Desc.value).toBe("");
  });

  it("picking a suggestion fills the row's description and category, leaving siblings untouched", async () => {
    const recorded: RecordedCall[] = [];
    makeApiMock(recorded);
    render(<BatchEntryPage />);

    await waitFor(() => screen.getByLabelText("Row 1 description"));

    const row1Desc = screen.getByLabelText("Row 1 description") as HTMLInputElement;
    fireEvent.change(row1Desc, { target: { value: "Albert" } });
    await flushDebounce();

    // The dropdown's first option has role="option" with the
    // suggestion's description as text.
    const option = await screen.findByRole("option", { name: /Albert Heijn/ });
    // The component commits on mousedown.
    fireEvent.mouseDown(option);

    // Row 1's description took the suggestion's value.
    await waitFor(() => {
      expect(
        (screen.getByLabelText("Row 1 description") as HTMLInputElement).value,
      ).toBe("Albert Heijn");
    });

    // Row 1's category got pre-filled from the suggestion.
    const row1Cat = screen.getByLabelText("Row 1 category") as HTMLSelectElement;
    expect(row1Cat.value).toBe("5");

    // Row 2 is completely untouched.
    expect(
      (screen.getByLabelText("Row 2 description") as HTMLInputElement).value,
    ).toBe("");
    expect(
      (screen.getByLabelText("Row 2 category") as HTMLSelectElement).value,
    ).toBe("");
  });

  it("does not overwrite a category the user already picked", async () => {
    const recorded: RecordedCall[] = [];
    makeApiMock(recorded);
    render(<BatchEntryPage />);

    await waitFor(() => screen.getByLabelText("Row 1 description"));

    // User picks a non-default category FIRST.
    const row1Cat = screen.getByLabelText("Row 1 category") as HTMLSelectElement;
    fireEvent.change(row1Cat, { target: { value: "6" } });
    expect(row1Cat.value).toBe("6");

    // Then types into the description and picks a suggestion whose
    // top category is 5.
    const row1Desc = screen.getByLabelText("Row 1 description");
    fireEvent.change(row1Desc, { target: { value: "Albert" } });
    await flushDebounce();

    const option = await screen.findByRole("option", { name: /Albert Heijn/ });
    fireEvent.mouseDown(option);

    // Description still got picked, but category stays as user choice.
    await waitFor(() => {
      expect(
        (screen.getByLabelText("Row 1 description") as HTMLInputElement).value,
      ).toBe("Albert Heijn");
    });
    expect(
      (screen.getByLabelText("Row 1 category") as HTMLSelectElement).value,
    ).toBe("6");
  });

  it("Tab from a row's description does not jump to the next row", async () => {
    const recorded: RecordedCall[] = [];
    makeApiMock(recorded);
    render(<BatchEntryPage />);

    await waitFor(() => screen.getByLabelText("Row 1 description"));

    const row1Desc = screen.getByLabelText("Row 1 description");
    const row2Desc = screen.getByLabelText("Row 2 description");
    row1Desc.focus();
    expect(document.activeElement).toBe(row1Desc);

    // Component's keydown handler should NOT preventDefault on Tab —
    // it simply closes the dropdown so the browser's native focus
    // traversal lands on the next focusable cell (row 1 amount), not
    // on row 2's description.
    const ev = fireEvent.keyDown(row1Desc, { key: "Tab" });
    // fireEvent returns true when the event was not preventDefault'd.
    expect(ev).toBe(true);
    // The autocomplete didn't yank focus to row 2.
    expect(document.activeElement).not.toBe(row2Desc);
  });

  it("submits a batch of rows populated via the autocomplete", async () => {
    const recorded: RecordedCall[] = [];
    makeApiMock(recorded);
    render(<BatchEntryPage />);

    await waitFor(() => screen.getByLabelText("Row 1 description"));

    // Pick a suggestion for row 1's description + category.
    const row1Desc = screen.getByLabelText("Row 1 description");
    fireEvent.change(row1Desc, { target: { value: "Albert" } });
    await flushDebounce();
    const option = await screen.findByRole("option", {
      name: /Albert Heijn/,
    });
    fireEvent.mouseDown(option);

    await waitFor(() => {
      expect(
        (screen.getByLabelText("Row 1 description") as HTMLInputElement).value,
      ).toBe("Albert Heijn");
    });

    // Fill the remaining row 1 cells so the row is submittable.
    fireEvent.change(screen.getByLabelText("Row 1 amount"), {
      target: { value: "12.50" },
    });
    fireEvent.change(screen.getByLabelText("Row 1 account"), {
      target: { value: "10" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Submit 1 row$/ }));

    await waitFor(() => {
      expect(screen.getByLabelText("Row 1 imported")).toBeTruthy();
    });

    // Verify the batch payload carried the suggestion's category.
    const batchCall = vi
      .mocked(apiFetch)
      .mock.calls.find(([p]) => p === "/api/v1/transactions/batch");
    expect(batchCall).toBeDefined();
    const body = JSON.parse((batchCall![1] as RequestInit).body as string);
    expect(body.rows).toHaveLength(1);
    expect(body.rows[0].transaction.description).toBe("Albert Heijn");
    expect(body.rows[0].transaction.category_id).toBe(5);
  });
});
