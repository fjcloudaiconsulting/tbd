/**
 * Email-first sign-up, step 2 (TBD-654): /signup/complete.
 *
 * Pins: the token is read once and dropped from the address bar (FE-03), the
 * page never calls the API on load (only the POST redeems the link), the
 * success path signs in and lands in /dashboard, and every error the backend
 * can answer has its own place on the page.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import SignupCompletePage from "@/app/signup/complete/page";
import { ApiResponseError } from "@/lib/api";

const apiFetchMock = vi.fn();
const signInMock = vi.fn();
const refreshMeMock = vi.fn();
const pushMock = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  signInWithAccessToken: (...args: unknown[]) => signInMock(...args),
}));
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({ refreshMe: refreshMeMock }),
}));
// Reads the live URL, as Next.js does: once the page strips ?token= from the
// address bar, useSearchParams no longer returns it.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
  useRouter: () => ({ push: pushMock }),
}));

const COMPLETE = "/api/v1/auth/signup/complete";

/** `complete` is what /signup/complete does: resolve or throw. */
function mockApi(complete: () => unknown) {
  apiFetchMock.mockImplementation(async (path: string) => {
    if (path.startsWith("/api/v1/auth/check-username")) return { available: true, suggestion: null };
    if (path === COMPLETE) return complete();
    throw new Error(`unexpected call ${path}`);
  });
}

async function fillAndSubmit({ username = "alice", password2 = "supersafe-pass-1", orgName = "" } = {}) {
  fireEvent.change(screen.getByLabelText(/username/i), { target: { value: username } });
  // Settle the debounced availability check inside act().
  await screen.findByText("Available", {}, { timeout: 2000 });
  fireEvent.change(screen.getByLabelText(/organization/i), { target: { value: orgName } });
  fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "supersafe-pass-1" } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: password2 } });
  // The submit settles in a microtask after the click; keep it inside act().
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
  });
}

function completeBody() {
  const call = apiFetchMock.mock.calls.find(([p]) => p === COMPLETE);
  return JSON.parse((call![1] as { body: string }).body);
}

beforeEach(() => {
  apiFetchMock.mockReset();
  signInMock.mockReset();
  refreshMeMock.mockReset().mockResolvedValue(undefined);
  pushMock.mockReset();
  window.history.replaceState(null, "", "/signup/complete?token=signup-tok-123");
});

describe("/signup/complete — the link", () => {
  it("drops the token from the address bar and calls nothing on load", async () => {
    mockApi(() => ({ access_token: "a" }));
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    await waitFor(() => expect(window.location.search).toBe(""));
    expect(window.location.pathname).toBe("/signup/complete");
    expect(apiFetchMock).not.toHaveBeenCalled();
  });

  it("shows the expired message when there is no token", async () => {
    window.history.replaceState(null, "", "/signup/complete");
    render(<SignupCompletePage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/expired or was already used/i);
    expect(screen.getByRole("link", { name: /start signing up again/i })).toHaveAttribute("href", "/register");
    expect(screen.queryByLabelText(/username/i)).toBeNull();
  });
});

describe("/signup/complete — submit", () => {
  it("creates the account with the stripped token, signs in and lands in /dashboard", async () => {
    mockApi(() => ({ access_token: "access-1" }));
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    fireEvent.change(screen.getByLabelText(/first name/i), { target: { value: "Alice" } });
    await fillAndSubmit({ orgName: "Alice Household" });

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
    expect(completeBody()).toEqual({
      token: "signup-tok-123",
      username: "alice",
      password: "supersafe-pass-1",
      first_name: "Alice",
      org_name: "Alice Household",
    });
    expect(signInMock).toHaveBeenCalledWith("access-1");
    expect(refreshMeMock).toHaveBeenCalled();
  });

  it("still lands in /dashboard when loading the user fails after the account exists", async () => {
    mockApi(() => ({ access_token: "access-1" }));
    refreshMeMock.mockRejectedValue(new Error("timeout"));
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    await fillAndSubmit();
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
  });

  it("refuses mismatched passwords without calling the API", async () => {
    mockApi(() => ({ access_token: "a" }));
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    await fillAndSubmit({ password2: "different-pass-1" });
    expect(await screen.findByRole("alert")).toHaveTextContent(/passwords do not match/i);
    expect(apiFetchMock.mock.calls.some(([p]) => p === COMPLETE)).toBe(false);
  });

  it("an expired or spent link replaces the form and links to /register", async () => {
    mockApi(() => {
      throw new ApiResponseError(400, "This link has expired or was already used.", "invalid_signup_token");
    });
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    await fillAndSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(/expired or was already used/i);
    expect(screen.getByRole("link", { name: /start signing up again/i })).toHaveAttribute("href", "/register");
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("account_exists links to /login", async () => {
    mockApi(() => {
      throw new ApiResponseError(409, "This email already has an account. Sign in instead.", "account_exists");
    });
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    await fillAndSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(/already has an account/i);
    expect(screen.getByRole("link", { name: /sign in instead/i })).toHaveAttribute("href", "/login");
  });

  it("username_taken shows in place with the suggestion, and a resubmit reuses the link", async () => {
    let calls = 0;
    mockApi(() => {
      calls += 1;
      if (calls === 1) {
        throw new ApiResponseError(409, "That username is taken.", "username_taken", {
          code: "username_taken",
          message: "That username is taken.",
          suggestion: "alice2",
        });
      }
      return { access_token: "access-2" };
    });
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    await fillAndSubmit();

    const field = screen.getByLabelText(/username/i);
    await waitFor(() => expect(field).toHaveFocus());
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/that username is taken/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "alice2" }));
    expect(field).toHaveValue("alice2");
    await waitFor(() => expect(screen.getByText("Available")).toBeInTheDocument());
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    });
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
    const posts = apiFetchMock.mock.calls.filter(([p]) => p === COMPLETE);
    expect(JSON.parse((posts[1][1] as { body: string }).body)).toMatchObject({
      token: "signup-tok-123",
      username: "alice2",
    });
  });

  it("org_name_taken shows under the organization field", async () => {
    mockApi(() => {
      throw new ApiResponseError(409, "An organization with that name already exists.", "org_name_taken");
    });
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    await fillAndSubmit({ orgName: "Taken Org" });

    const org = screen.getByLabelText(/organization/i);
    await waitFor(() => expect(org).toHaveFocus());
    expect(org).toHaveAttribute("aria-invalid", "true");
    expect(org).toHaveAccessibleDescription(/organization with that name already exists/i);
  });

  it("any other failure is announced and keeps the form", async () => {
    mockApi(() => {
      throw new ApiResponseError(409, "Registration conflict, please try again");
    });
    render(<SignupCompletePage />);
    await screen.findByLabelText(/username/i);
    await fillAndSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(/registration conflict/i);
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
  });
});
