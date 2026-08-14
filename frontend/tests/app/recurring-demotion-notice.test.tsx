// TBD-312. Stopping or deleting a recurring template deletes its pending
// future rows, and deleting a row that another row was reconcile-matched
// AGAINST marks that other row REJECTED. REJECTED is terminal and
// unreachable through the edit API, so the change is irreversible and only
// direct SQL recovers it.
//
// Before this ticket the recurring page rendered only
// "N pending transaction(s) removed." The user could irreversibly reject a
// matched duplicate, removing its amount from every balance and every report
// permanently, and be told nothing about it. The transactions page has said
// so since TBD-294.
//
// Nothing fenced the recurring page's success copy at all, so this file is
// the path fence, not just an item fence: it drives the real Stop and Delete
// interactions rather than calling the helper directly.
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { demotionNotice } from "@/lib/demotion";
import type { RecurringTransaction } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/recurring",
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner",
  org_id: 1, org_name: "Acme", billing_cycle_day: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, password_set: true,
  subscription_status: null, subscription_plan: null, trial_end: null,
  allow_manual_balance_adjustment: false,
};

function rec(over: Partial<RecurringTransaction> = {}): RecurringTransaction {
  return {
    id: 1, account_id: 1, account_name: "Checking", category_id: 1,
    category_name: "Bills", description: "Rent", amount: 1200,
    type: "expense", frequency: "monthly", next_due_date: "2026-09-01",
    auto_settle: false, is_active: true, occurrence_count: null,
    occurrences_elapsed: 0, ...over,
  };
}

/** Mock the list plus one mutation response for the stop/delete call. */
function mockApi(mutationResponse: unknown) {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    if (url === "/api/v1/recurring" && !init) {
      return Promise.resolve([rec()]);
    }
    return Promise.resolve(mutationResponse);
  }) as never);
}

function setAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.localStorage.clear();
  setAuth();
});

/** Click the row's Stop button, then confirm inside the modal.
 *
 * The row button and the modal's confirm button are BOTH named "Stop", so
 * the confirm lookup is scoped to the dialog. A bare
 * `findByRole("button", {name: "Stop"})` matches two elements and throws.
 */
async function stopAndConfirm() {
  const stop = await screen.findByLabelText("Stop: Rent");
  fireEvent.click(stop);
  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: "Stop" });
  fireEvent.click(confirm);
}

/** Click the row's Delete button, then confirm inside the modal.
 *
 * ⚠ This helper exists because `doDelete` had NO fence in the first draft
 * while its own comment claimed "Fenced separately". Reverting `doDelete`
 * to the pre-TBD-312 message left the entire frontend suite green. Both
 * routes reach the demotion by different paths, and this repo's most
 * repeated defect is fixing one sibling and not the other.
 */
async function deleteAndConfirm() {
  const del = await screen.findByLabelText("Delete: Rent");
  fireEvent.click(del);
  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: "Delete" });
  fireEvent.click(confirm);
}

describe("recurring page — irreversible demotion is reported (TBD-312)", () => {
  it("fence: stopping a template announces a demoted duplicate", async () => {
    mockApi({ stopped: true, pending_removed: 1, demoted_ids: [4242] });
    render(<RecurringPage />);
    await stopAndConfirm();

    // Kills: rendering only `${pending_removed} pending transaction(s)
    // removed.` and dropping the demotion, which is what shipped.
    await waitFor(() => {
      expect(
        screen.getByText(/matched duplicate was marked rejected/i),
      ).toBeInTheDocument();
    });
    // The lead-in must SURVIVE, not be replaced by the demotion sentence.
    expect(
      screen.getByText(/1 pending transaction\(s\) removed/i),
    ).toBeInTheDocument();
  });

  it("fence: the wording is identical to the transactions page", async () => {
    mockApi({ stopped: true, pending_removed: 1, demoted_ids: [4242] });
    render(<RecurringPage />);
    await stopAndConfirm();

    // What this ACTUALLY kills: the shared module being reworded while the
    // page keeps a private copy, which the hardcoded-literal assertion in
    // the test above would not see.
    //
    // What it does NOT kill, stated so the next reader does not stop
    // looking: a page that detaches from `@/lib/demotion` and inlines a
    // byte-identical sentence still passes. The anchor against that is the
    // literal above plus the transactions page's own literal in
    // transactions-matched-row-actions.test.tsx.
    const shared = demotionNotice([4242]);
    expect(shared).not.toBe("");
    await waitFor(() => {
      expect(screen.getByText(new RegExp(shared.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))).toBeInTheDocument();
    });
  });

  it("control: a stop that demoted nothing says nothing about demotions", async () => {
    mockApi({ stopped: true, pending_removed: 2, demoted_ids: [] });
    render(<RecurringPage />);
    await stopAndConfirm();

    await waitFor(() => {
      expect(
        screen.getByText(/2 pending transaction\(s\) removed/i),
      ).toBeInTheDocument();
    });
    // Over-reach fence: announcing a rejection that did not happen is as
    // wrong as staying silent about one that did.
    expect(
      screen.queryByText(/marked rejected/i),
    ).not.toBeInTheDocument();
  });

  it("control: a response with no demoted_ids field does not crash or misreport", async () => {
    // The field is optional on the client type, so an older server (or a
    // cached response) must degrade to the pre-TBD-312 message rather than
    // rendering "undefined".
    mockApi({ stopped: true, pending_removed: 1 });
    render(<RecurringPage />);
    await stopAndConfirm();

    await waitFor(() => {
      expect(
        screen.getByText(/1 pending transaction\(s\) removed/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/undefined/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/marked rejected/i)).not.toBeInTheDocument();
  });
});

describe("recurring page — the DELETE sibling reports it too (TBD-312)", () => {
  it("fence: deleting a template announces a demoted duplicate", async () => {
    mockApi({ deleted: true, pending_removed: 1, demoted_ids: [4242] });
    render(<RecurringPage />);
    await deleteAndConfirm();

    // Kills reverting `doDelete` to the pre-TBD-312 message. Proven: with
    // only the Stop fences present, that revert left 2885 tests green.
    await waitFor(() => {
      expect(
        screen.getByText(/matched duplicate was marked rejected/i),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText(/1 pending transaction\(s\) removed/i),
    ).toBeInTheDocument();
  });

  it("control: a delete that demoted nothing says nothing about demotions", async () => {
    mockApi({ deleted: true, pending_removed: 3, demoted_ids: [] });
    render(<RecurringPage />);
    await deleteAndConfirm();

    await waitFor(() => {
      expect(
        screen.getByText(/3 pending transaction\(s\) removed/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/marked rejected/i)).not.toBeInTheDocument();
  });
});
