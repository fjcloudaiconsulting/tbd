/**
 * Email-first sign-up, step 1 (TBD-654): the /register page.
 *
 * Pins:
 *   * Only the address (and the captcha token) goes to /signup/start; there is
 *     no username, password or /check-username call on this page.
 *   * Every 202 renders the same "Check your email" panel. The backend's 202
 *     is identical for every address state, and so is the UI.
 *   * The per-IP limit (429) offers "try again later" and Google sign-in, in
 *     the same words for every address.
 *   * Captcha: the widget resets on code=captcha_failed only (PR #327 review),
 *     keyed off the code, never the message.
 *   * A pending install goes to /setup.
 */
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import RegisterPageBody from "@/components/auth/RegisterPageBody";
import { ApiResponseError } from "@/lib/api";

// ── module mocks ────────────────────────────────────────────────────────────

const apiFetchMock = vi.fn();
const replaceMock = vi.fn();
let needsSetup = false;

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({ user: null, loading: false, needsSetup }),
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
  useRouter: () => ({ replace: replaceMock }),
}));

// Replace the Turnstile widget with a thin double that exposes the same
// imperative ref + onSuccess wiring the production widget does. Keeps the
// test off the network and out of the Cloudflare loader script.
const turnstileResetMock = vi.fn();
type TurnstileSuccessHandler = (token: string) => void;

vi.mock("@marsidev/react-turnstile", async () => {
  // vi.mock factories are hoisted ABOVE top-level imports, so a normal
  // ``import React from "react"`` at the top would be undefined here.
  const React = await vi.importActual<typeof import("react")>("react");
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

const START = "/api/v1/auth/signup/start";

/** `start` is what /signup/start does: resolve (the 202) or throw. */
function mockApi(captchaRequired: boolean, start: () => unknown = () => ({ detail: "ok" })) {
  apiFetchMock.mockImplementation(async (path: string) => {
    if (path.startsWith("/api/v1/auth/status")) {
      return { needs_setup: false, captcha_required: captchaRequired };
    }
    if (path === START) return start();
    throw new Error(`unexpected call ${path}`);
  });
}

async function renderPage(captchaRequired: boolean) {
  render(<RegisterPageBody cspNonce="test-nonce" />);
  if (captchaRequired) {
    await screen.findByTestId("turnstile-mock-solve");
  } else {
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/auth/status"));
  }
}

function submit(email = "alice@example.com") {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: email } });
  fireEvent.click(screen.getByRole("button", { name: /continue/i }));
}

function startCalls() {
  return apiFetchMock.mock.calls.filter(([p]) => p === START);
}

const rateLimited = () => {
  throw new ApiResponseError(429, "Request failed");
};

beforeEach(() => {
  apiFetchMock.mockReset();
  replaceMock.mockReset();
  turnstileResetMock.mockReset();
  needsSetup = false;
  // The widget render condition AND-s captcha_required from /auth/status
  // with a non-empty TBD_CAPTCHA_SITE_KEY. Set a value here so the
  // mocked status response is the only switch each test flips.
  vi.stubEnv("TBD_CAPTCHA_SITE_KEY", "1x00000000000000000000BB");
  vi.stubEnv("TBD_GOOGLE_SSO_ENABLED", "true");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

// ── tests ───────────────────────────────────────────────────────────────────

describe("RegisterPageBody — the form", () => {
  it("asks for the email only", async () => {
    mockApi(false);
    await renderPage(false);
    expect(screen.getByLabelText(/email/i)).toBeRequired();
    expect(screen.queryByLabelText(/username/i)).toBeNull();
    expect(screen.queryByLabelText(/password/i)).toBeNull();
    expect(screen.queryByLabelText(/organization/i)).toBeNull();
  });

  it("posts the address alone to /signup/start, never /check-username", async () => {
    mockApi(false);
    await renderPage(false);
    submit();
    await screen.findByRole("heading", { name: /check your email/i });

    expect(startCalls()).toHaveLength(1);
    const body = JSON.parse((startCalls()[0][1] as { body: string }).body);
    expect(body).toEqual({ email: "alice@example.com" });
    expect(apiFetchMock.mock.calls.some(([p]) => String(p).includes("check-username"))).toBe(false);
  });

  it("redirects to /setup while the install has no user", async () => {
    needsSetup = true;
    mockApi(false);
    await renderPage(false);
    expect(replaceMock).toHaveBeenCalledWith("/setup");
  });

  it("redirects to /setup when the backend answers setup_required", async () => {
    mockApi(false, () => {
      throw new ApiResponseError(403, "Finish setting up this install first.", "setup_required");
    });
    await renderPage(false);
    submit();
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/setup"));
  });

  it("describes the email field by its hint", async () => {
    mockApi(false);
    await renderPage(false);
    const field = screen.getByLabelText(/email/i);
    expect(field).toHaveAccessibleDescription(/email you a link/i);
    expect(field).not.toHaveAttribute("aria-invalid");
  });

  it("announces a validation error and ties it to the email field", async () => {
    mockApi(false, () => {
      throw new ApiResponseError(422, "email: value is not a valid email address");
    });
    await renderPage(false);
    submit("alice@example.com");
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a valid email/i);
    const field = screen.getByLabelText(/email/i);
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(/not a valid email/i);
  });
});

describe("RegisterPageBody — check your email", () => {
  it("shows the same panel, with the address, and moves focus to it", async () => {
    mockApi(false);
    await renderPage(false);
    submit("bob@example.com");

    const heading = await screen.findByRole("heading", { name: /check your email/i });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByRole("status")).toHaveTextContent("bob@example.com");
    // The help is the same for every address: a capped address also gets a 202.
    expect(screen.getByText(/at most one email to an address every 10 minutes/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign up with google/i })).toBeInTheDocument();
  });

  it("renders the same panel text for any two addresses (no per-state screen)", async () => {
    mockApi(false);
    const { container, unmount } = render(<RegisterPageBody cspNonce="n" />);
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/auth/status"));
    submit("new@example.com");
    await screen.findByRole("heading", { name: /check your email/i });
    const first = container.textContent?.replaceAll("new@example.com", "X");
    unmount();

    const second = render(<RegisterPageBody cspNonce="n" />);
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(3));
    submit("existing@example.com");
    await screen.findByRole("heading", { name: /check your email/i });
    expect(second.container.textContent?.replaceAll("existing@example.com", "X")).toBe(first);
  });

  it("'Use a different email' returns to the form with the email focused", async () => {
    mockApi(false);
    await renderPage(false);
    submit();
    await screen.findByRole("heading", { name: /check your email/i });

    fireEvent.click(screen.getByRole("button", { name: /use a different email/i }));
    const field = screen.getByLabelText(/email/i);
    expect(field).toHaveValue("alice@example.com");
    await waitFor(() => expect(field).toHaveFocus());
  });
});

describe("RegisterPageBody — too many attempts (429)", () => {
  it("offers try-again-later and Google sign-in, and stays on the form", async () => {
    mockApi(false, rateLimited);
    await renderPage(false);
    submit();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/try again later, or sign up with google instead/i);
    expect(screen.getByRole("button", { name: /sign up with google/i })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /check your email/i })).toBeNull();
  });

  it("uses the same words for any address", async () => {
    mockApi(false, rateLimited);
    await renderPage(false);
    submit("new@example.com");
    const first = (await screen.findByRole("alert")).textContent;
    submit("existing@example.com");
    await waitFor(() => expect(startCalls()).toHaveLength(2));
    expect((await screen.findByRole("alert")).textContent).toBe(first);
  });

  it("says only 'try again later' when Google sign-in is off", async () => {
    vi.stubEnv("TBD_GOOGLE_SSO_ENABLED", "false");
    mockApi(false, rateLimited);
    await renderPage(false);
    submit();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/try again later\.$/i);
    expect(screen.queryByRole("button", { name: /google/i })).toBeNull();
  });
});

describe("RegisterPageBody — captcha", () => {
  it("does not render the widget when /auth/status reports captcha_required=false", async () => {
    mockApi(false);
    await renderPage(false);
    expect(screen.queryByTestId("captcha-widget")).toBeNull();
  });

  it("renders the widget when captcha_required=true and forwards the token", async () => {
    mockApi(true);
    await renderPage(true);
    fireEvent.click(screen.getByTestId("turnstile-mock-solve"));
    submit();
    await screen.findByRole("heading", { name: /check your email/i });
    const body = JSON.parse((startCalls()[0][1] as { body: string }).body);
    expect(body.captcha_token).toBe("mock-turnstile-token");
  });

  it("resets the widget on code=captcha_failed and shows the message", async () => {
    mockApi(true, () => {
      throw new ApiResponseError(400, "Could not verify you are human. Please try again.", "captcha_failed");
    });
    await renderPage(true);
    fireEvent.click(screen.getByTestId("turnstile-mock-solve"));
    submit();
    await waitFor(() => expect(turnstileResetMock).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("alert")).toHaveTextContent(/could not verify you are human/i);
  });

  it("resets the widget on a 5xx, since the token was already spent", async () => {
    mockApi(true, () => {
      throw new ApiResponseError(503, "Service unavailable");
    });
    await renderPage(true);
    fireEvent.click(screen.getByTestId("turnstile-mock-solve"));
    submit();
    await waitFor(() => expect(turnstileResetMock).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("alert")).toHaveTextContent(/service unavailable/i);
    expect(screen.getByLabelText(/email/i)).not.toHaveAttribute("aria-invalid");
  });

  it("does NOT reset the widget on other failures (e.g. 429)", async () => {
    mockApi(true, rateLimited);
    await renderPage(true);
    fireEvent.click(screen.getByTestId("turnstile-mock-solve"));
    submit();
    await screen.findByRole("alert");
    expect(turnstileResetMock).not.toHaveBeenCalled();
  });
});
