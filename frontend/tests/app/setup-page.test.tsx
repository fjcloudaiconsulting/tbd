/**
 * First-user setup (INF-05): the /setup page asks for SETUP_TOKEN only when
 * /auth/status says production needs it, forwards it to register(), and on a
 * setup_token_invalid answer shows the field, focused and marked invalid.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import SetupPage from "@/app/setup/page";
import { ApiResponseError } from "@/lib/api";

const apiFetchMock = vi.fn();
const registerMock = vi.fn();
const loginMock = vi.fn();
const pushMock = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({ needsSetup: true, loading: false, register: registerMock, login: loginMock }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
}));

function mockStatus(setupTokenRequired: boolean) {
  apiFetchMock.mockResolvedValue({ needs_setup: true, setup_token_required: setupTokenRequired });
}

async function renderPage() {
  render(<SetupPage />);
  await waitFor(() => expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/auth/status"));
  // Let the status answer land inside act().
  await act(async () => {});
}

async function fillAndSubmit() {
  fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "founder" } });
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "founder@example.com" } });
  fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "founder-pass-1" } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "founder-pass-1" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /create admin account/i }));
  });
}

beforeEach(() => {
  apiFetchMock.mockReset();
  registerMock.mockReset().mockResolvedValue(undefined);
  loginMock.mockReset().mockResolvedValue(undefined);
  pushMock.mockReset();
});

describe("/setup — the bootstrap token", () => {
  it("has no token field when the backend does not ask for one", async () => {
    mockStatus(false);
    await renderPage();
    expect(screen.queryByLabelText(/setup token/i)).toBeNull();
    await fillAndSubmit();
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
    expect(registerMock.mock.calls[0][6]).toBeUndefined();
  });

  it("asks for the token when required and forwards it to register()", async () => {
    mockStatus(true);
    await renderPage();
    const field = screen.getByLabelText(/setup token/i);
    expect(field).toBeRequired();
    expect(field).toHaveAccessibleDescription(/SETUP_TOKEN value set on the server/i);
    fireEvent.change(field, { target: { value: "operator-token" } });
    await fillAndSubmit();
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
    expect(registerMock.mock.calls[0][6]).toBe("operator-token");
  });

  it("on setup_token_invalid shows the field, focused, invalid and described by the error", async () => {
    mockStatus(false);
    registerMock.mockRejectedValue(
      new ApiResponseError(403, "The setup token is missing or wrong.", "setup_token_invalid"),
    );
    await renderPage();
    await fillAndSubmit();

    expect(await screen.findByRole("alert")).toHaveTextContent(/setup token is missing or wrong/i);
    const field = screen.getByLabelText(/setup token/i);
    await waitFor(() => expect(field).toHaveFocus());
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(/setup token is missing or wrong/i);
    expect(loginMock).not.toHaveBeenCalled();
  });
});
