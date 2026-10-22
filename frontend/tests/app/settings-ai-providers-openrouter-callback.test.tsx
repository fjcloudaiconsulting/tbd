import React, { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import OpenRouterCallbackPage from "@/app/settings/ai-providers/openrouter/callback/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { ApiResponseError, ApiTimeoutError, apiFetch } from "@/lib/api";
import { OPENROUTER_NONCE_KEY, startOpenRouterConnect } from "@/lib/openrouter-connect";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/settings/ai-providers/openrouter/callback",
}));
vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: vi.fn() }));
vi.mock("@/lib/hooks/use-ai-status", () => ({ useAiStatus: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});
vi.mock("@/lib/openrouter-connect", async () => {
  const actual = await vi.importActual<typeof import("@/lib/openrouter-connect")>(
    "@/lib/openrouter-connect",
  );
  return { ...actual, startOpenRouterConnect: vi.fn() };
});

const b64url = (s: string) => Buffer.from(s).toString("base64url");
const STATE = `${b64url('{"alg":"HS256"}')}.${b64url('{"jti":"ab12cd34ef56"}')}.sig`;
const NONCE = "n".repeat(43);
const COMPLETE = "/api/v1/settings/ai-providers/oauth/openrouter/complete";

function auth(role: "admin" | "member" = "admin", loading = false) {
  vi.mocked(useAuth).mockReturnValue({
    user: loading ? null : { id: 1, org_id: 1, role },
    loading,
  } as never);
}

function visit(query: string) {
  window.history.replaceState(null, "", `/settings/ai-providers/openrouter/callback${query}`);
}

const completeCalls = () =>
  vi.mocked(apiFetch).mock.calls.filter(([url]) => url === COMPLETE);

describe("OpenRouter callback page", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.mocked(apiFetch).mockReset();
    vi.mocked(startOpenRouterConnect).mockReset();
    auth();
  });

  it("strips code and state from the URL before the exchange settles", () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockReturnValue(new Promise(() => {}) as never);
    visit(`?code=c1&state=${STATE}`);

    render(<OpenRouterCallbackPage />);

    expect(window.location.search).toBe("");
    expect(screen.getByRole("heading", { name: "Connecting OpenRouter" })).toBeInTheDocument();
  });

  it("strips the URL even while auth is still loading", () => {
    auth("admin", true);
    visit(`?code=c1&state=${STATE}`);

    render(<OpenRouterCallbackPage />);

    expect(window.location.search).toBe("");
  });

  it("gives the exchange longer than the server's worst case", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockReturnValue(new Promise(() => {}) as never);
    visit(`?code=c1&state=${STATE}`);

    render(<OpenRouterCallbackPage />);

    await waitFor(() => expect(completeCalls()).toHaveLength(1));
    expect((completeCalls()[0][1] as { timeoutMs?: number }).timeoutMs).toBeGreaterThanOrEqual(30_000);
  });

  it("completes once with the code, state and this tab's nonce, then spends the nonce", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockResolvedValue({ id: 7, label: "OpenRouter (ab12cd34)" } as never);
    visit(`?code=c1&state=${STATE}`);

    render(
      <StrictMode>
        <OpenRouterCallbackPage />
      </StrictMode>,
    );

    expect(await screen.findByRole("heading", { name: "OpenRouter connected" })).toBeInTheDocument();
    expect(completeCalls()).toHaveLength(1);
    expect(JSON.parse(completeCalls()[0][1]!.body as string)).toEqual({
      code: "c1",
      state: STATE,
      nonce: NONCE,
    });
    expect(sessionStorage.getItem(OPENROUTER_NONCE_KEY)).toBeNull();
    expect(screen.getByText(/OpenRouter \(ab12cd34\)/)).toBeInTheDocument();
    expect(screen.getByText(/zero data retention/i)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open OpenRouter privacy settings" }),
    ).toHaveAttribute("href", "https://openrouter.ai/settings/privacy");
  });

  it("a re-run effect does not mistake the spent nonce for another tab", () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockReturnValue(new Promise(() => {}) as never);
    visit(`?code=c1&state=${STATE}`);

    render(
      <StrictMode>
        <OpenRouterCallbackPage />
      </StrictMode>,
    );

    expect(screen.getByRole("heading", { name: "Connecting OpenRouter" })).toBeInTheDocument();
  });

  it("waits for auth before completing", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockResolvedValue({ id: 7, label: "x" } as never);
    visit(`?code=c1&state=${STATE}`);
    auth("admin", true);

    const { rerender } = render(<OpenRouterCallbackPage />);
    expect(completeCalls()).toHaveLength(0);

    auth();
    rerender(<OpenRouterCallbackPage />);
    await waitFor(() => expect(completeCalls()).toHaveLength(1));
  });

  it("does not complete in a tab that did not start the flow", async () => {
    visit(`?code=c1&state=${STATE}`);

    render(<OpenRouterCallbackPage />);

    expect(
      await screen.findByRole("heading", { name: "Start the connection again" }),
    ).toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("a bare URL (Back or reload after the strip) never claims nothing was saved", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    visit("");

    render(<OpenRouterCallbackPage />);

    expect(await screen.findByRole("heading", { name: "Nothing to finish here" })).toBeInTheDocument();
    expect(screen.queryByText(/Nothing was saved/)).not.toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("reads a code without state as cancelled", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    visit("?code=c1");

    render(<OpenRouterCallbackPage />);

    expect(await screen.findByRole("heading", { name: "Connection cancelled" })).toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("reads a callback without state as cancelled and spends the nonce", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    visit("?error=access_denied");

    render(<OpenRouterCallbackPage />);

    expect(await screen.findByRole("heading", { name: "Connection cancelled" })).toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(OPENROUTER_NONCE_KEY)).toBeNull();
  });

  it("does nothing for a non-admin", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    visit(`?code=c1&state=${STATE}`);
    auth("member");

    render(<OpenRouterCallbackPage />);

    expect(await screen.findByText(/need admin or owner role/i)).toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("sends a busy user back to Connect, never re-completing the claimed state", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockRejectedValue(new ApiResponseError(503, "busy", "openrouter_busy"));
    visit(`?code=c1&state=${STATE}`);

    render(<OpenRouterCallbackPage />);

    expect(await screen.findByRole("heading", { name: "OpenRouter is busy" })).toBeInTheDocument();
    expect(screen.queryByText(/may have been created/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Connect again" }));
    expect(startOpenRouterConnect).toHaveBeenCalledTimes(1);
    expect(completeCalls()).toHaveLength(1);
  });

  it("shows a failed restart and lets the user try again", async () => {
    vi.mocked(startOpenRouterConnect).mockRejectedValue(
      new ApiResponseError(429, "Too many attempts. Try again later.", "oauth_rate_limited"),
    );
    visit(`?code=c1&state=${STATE}`);

    render(<OpenRouterCallbackPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Connect again" }));

    expect(await screen.findByText("Too many attempts. Try again later.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect again" })).toBeEnabled();
  });

  it.each([
    [400, "oauth_state_invalid", "This connection link no longer works"],
    [409, "oauth_state_mismatch", "This connection link no longer works"],
    [409, "oauth_state_used", "This connection link no longer works"],
    [400, "openrouter_code_rejected", "This connection link no longer works"],
    [401, undefined, "This connection link no longer works"],
    [403, undefined, "This connection link no longer works"],
    [422, undefined, "This connection link no longer works"],
    [503, "refresh_transient", "Something went wrong on our side"],
    [429, "oauth_rate_limited", "Too many connection attempts"],
    [503, "oauth_unavailable", "Something went wrong on our side"],
  ])("a refusal before the exchange (%s %s) says nothing was saved", async (status, code, title) => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockRejectedValue(new ApiResponseError(status, "x", code));
    visit(`?code=c1&state=${STATE}`);

    render(<OpenRouterCallbackPage />);

    expect(await screen.findByRole("heading", { name: title })).toBeInTheDocument();
    expect(screen.getByText(/Nothing was saved/)).toBeInTheDocument();
    expect(screen.queryByText(/may have been created/)).not.toBeInTheDocument();
  });

  it.each([
    [400, "credential_validation_failed"],
    [502, "openrouter_unavailable"],
  ])("a failure after the exchange (%s %s) names the possibly orphaned key", async (status, code) => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockRejectedValue(new ApiResponseError(status, "x", code));
    visit(`?code=c1&state=${STATE}`);

    render(<OpenRouterCallbackPage />);

    expect(
      await screen.findByRole("heading", { name: "OpenRouter could not be connected" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/A key named "The Better Decision \(ab12cd34\)" may have been created/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open OpenRouter keys" })).toHaveAttribute(
      "href",
      "https://openrouter.ai/settings/keys",
    );
  });

  it.each([
    new TypeError("Failed to fetch"),
    new ApiTimeoutError(),
    new ApiResponseError(502, "Bad Gateway"),
    new ApiResponseError(500, "x", "something_new"),
  ])(
    "an unanswered or unreadable complete (%s) cannot say whether it worked",
    async (err) => {
      sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
      vi.mocked(apiFetch).mockRejectedValue(err);
      visit(`?code=c1&state=${STATE}`);

      render(<OpenRouterCallbackPage />);

      expect(
        await screen.findByRole("heading", { name: "We could not confirm the connection" }),
      ).toBeInTheDocument();
      expect(screen.getByText(/"The Better Decision \(ab12cd34\)"/)).toBeInTheDocument();
    },
  );

  it("names the key without a ref when the state carries none", async () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, NONCE);
    vi.mocked(apiFetch).mockRejectedValue(new TypeError("Failed to fetch"));
    visit(`?code=c1&state=${b64url("{}")}.${b64url('{"sub":"1"}')}.sig`);

    render(<OpenRouterCallbackPage />);

    expect(await screen.findByText(/named "The Better Decision" at/)).toBeInTheDocument();
  });
});
