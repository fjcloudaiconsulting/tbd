/**
 * Captcha integration on the register page.
 *
 * Pins the contract the architect flagged in the PR #327 review:
 *   * When the backend rejects with ApiResponseError(code="captcha_failed"),
 *     the widget MUST reset (Turnstile tokens are single-use, so a stale
 *     token in state would lock the user into a permanent rejection loop).
 *   * The reset behavior keys off the error CODE, never the user-facing
 *     message text — a copy edit must not silently break the gate.
 *   * When CAPTCHA is not required (/auth/status -> captcha_required=false),
 *     the widget never renders.
 */
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import RegisterPageBody from "@/components/auth/RegisterPageBody";
import { ApiResponseError } from "@/lib/api";

// ── module mocks ────────────────────────────────────────────────────────────

const registerMock = vi.fn();
const apiFetchMock = vi.fn();

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({
    user: null,
    loading: false,
    needsSetup: false,
    register: (...args: unknown[]) => registerMock(...args),
  }),
}));

vi.mock("@/lib/api", async () => {
  // Keep the real ApiResponseError class so `instanceof` checks fire.
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
}));

// Replace the Turnstile widget with a thin double that exposes the same
// imperative ref + onSuccess wiring the production widget does. Keeps the
// test off the network and out of the Cloudflare loader script.
const turnstileResetMock = vi.fn();
type TurnstileSuccessHandler = (token: string) => void;

vi.mock("@marsidev/react-turnstile", () => {
  const React = require("react") as typeof import("react");
  type Props = {
    onSuccess?: TurnstileSuccessHandler;
  };
  type Handle = { reset: () => void };
  const TurnstileMock = React.forwardRef<Handle, Props>(function TurnstileMock(
    { onSuccess },
    ref,
  ) {
    React.useImperativeHandle(ref, () => ({ reset: turnstileResetMock }), []);
    return (
      <button
        type="button"
        data-testid="turnstile-mock-solve"
        onClick={() => onSuccess?.("mock-turnstile-token")}
      >
        solve
      </button>
    );
  });
  return { Turnstile: TurnstileMock };
});

// ── helpers ─────────────────────────────────────────────────────────────────

function mockStatus(captchaRequired: boolean) {
  apiFetchMock.mockImplementation(async (path: string) => {
    if (typeof path === "string" && path.startsWith("/api/v1/auth/status")) {
      return { needs_setup: false, captcha_required: captchaRequired };
    }
    // username availability check — irrelevant to these tests
    return { available: true, suggestion: null };
  });
}

async function fillRequiredFields() {
  fireEvent.change(screen.getByLabelText(/username/i), {
    target: { value: "alice" },
  });
  fireEvent.change(screen.getByLabelText(/email/i), {
    target: { value: "alice@example.com" },
  });
  fireEvent.change(screen.getByLabelText(/^password$/i), {
    target: { value: "supersafe-password-1" },
  });
  fireEvent.change(screen.getByLabelText(/confirm password/i), {
    target: { value: "supersafe-password-1" },
  });
}

beforeEach(() => {
  registerMock.mockReset();
  apiFetchMock.mockReset();
  turnstileResetMock.mockReset();
  // The widget render condition AND-s captcha_required from /auth/status
  // with a non-empty NEXT_PUBLIC_CAPTCHA_SITE_KEY. Set a value here so the
  // mocked status response is the only switch each test flips.
  vi.stubEnv("NEXT_PUBLIC_CAPTCHA_SITE_KEY", "1x00000000000000000000BB");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

// ── tests ───────────────────────────────────────────────────────────────────

describe("RegisterPageBody — captcha", () => {
  it("does not render the widget when /auth/status reports captcha_required=false", async () => {
    mockStatus(false);
    render(<RegisterPageBody cspNonce="test-nonce" />);
    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/auth/status");
    });
    expect(screen.queryByTestId("captcha-widget")).toBeNull();
    expect(screen.queryByTestId("turnstile-mock-solve")).toBeNull();
  });

  it("renders the widget when captcha_required=true and forwards the token to register()", async () => {
    mockStatus(true);
    registerMock.mockResolvedValue(undefined);
    render(<RegisterPageBody cspNonce="test-nonce" />);

    await waitFor(() => {
      expect(screen.getByTestId("turnstile-mock-solve")).toBeInTheDocument();
    });

    await fillRequiredFields();
    fireEvent.click(screen.getByTestId("turnstile-mock-solve"));
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(registerMock).toHaveBeenCalled();
    });
    // 7th positional arg is captchaToken
    const args = registerMock.mock.calls[0];
    expect(args[6]).toBe("mock-turnstile-token");
  });

  it("resets the widget when register() throws ApiResponseError(code=captcha_failed)", async () => {
    mockStatus(true);
    registerMock.mockRejectedValue(
      new ApiResponseError(
        400,
        "Could not verify you are human. Please try again.",
        "captcha_failed",
      ),
    );
    render(<RegisterPageBody cspNonce="test-nonce" />);

    await waitFor(() => {
      expect(screen.getByTestId("turnstile-mock-solve")).toBeInTheDocument();
    });

    await fillRequiredFields();
    fireEvent.click(screen.getByTestId("turnstile-mock-solve"));
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(turnstileResetMock).toHaveBeenCalledTimes(1);
    });
    // The user-facing error MUST still surface so the user knows what to do.
    expect(
      screen.getByText(/could not verify you are human/i),
    ).toBeInTheDocument();
  });

  it("does NOT reset the widget on unrelated failures (e.g. 409 duplicate email)", async () => {
    mockStatus(true);
    registerMock.mockRejectedValue(
      new ApiResponseError(409, "Username or email already taken"),
    );
    render(<RegisterPageBody cspNonce="test-nonce" />);

    await waitFor(() => {
      expect(screen.getByTestId("turnstile-mock-solve")).toBeInTheDocument();
    });

    await fillRequiredFields();
    fireEvent.click(screen.getByTestId("turnstile-mock-solve"));
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(registerMock).toHaveBeenCalled();
    });
    expect(turnstileResetMock).not.toHaveBeenCalled();
  });
});
