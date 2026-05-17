/**
 * Google SSO callback page — first-run disclosure handoff.
 *
 * The page parses `#token=...&created_user=true` from the URL
 * fragment. The token gets handed to `setAccessToken` + `refreshMe`,
 * and the `created_user` flag (when present) is stashed in
 * sessionStorage under `tbd-sso-disclosure-pending` so the
 * onboarding wizard can render the privacy disclosure step.
 *
 * Returning SSO users land without `created_user=true`, the flag
 * stays absent, and the navigation target is `/dashboard` directly.
 */
import { render, waitFor } from "@testing-library/react";

import GoogleCallbackPage, {
  SSO_DISCLOSURE_PENDING_KEY,
} from "@/app/auth/google/callback/page";

const setAccessTokenMock = vi.fn();
const refreshMeMock = vi.fn(async () => {});
const replaceMock = vi.fn();

vi.mock("@/lib/api", () => ({
  setAccessToken: (...args: unknown[]) => setAccessTokenMock(...args),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({ refreshMe: refreshMeMock }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
}));

beforeEach(() => {
  setAccessTokenMock.mockReset();
  refreshMeMock.mockReset();
  refreshMeMock.mockResolvedValue(undefined as never);
  replaceMock.mockReset();
  try {
    window.sessionStorage.clear();
  } catch {
    // ignore
  }
  // Reset URL on the jsdom location.
  window.history.replaceState(null, "", "/auth/google/callback");
});

describe("GoogleCallbackPage", () => {
  it("stashes the disclosure flag and routes to /onboarding when created_user=true", async () => {
    window.history.replaceState(
      null,
      "",
      "/auth/google/callback#token=abc.def.ghi&created_user=true",
    );

    render(<GoogleCallbackPage />);

    await waitFor(() => {
      expect(setAccessTokenMock).toHaveBeenCalledWith("abc.def.ghi");
    });
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/onboarding");
    });
    expect(window.sessionStorage.getItem(SSO_DISCLOSURE_PENDING_KEY)).toBe("1");
  });

  it("returning SSO users (no created_user) go straight to /dashboard with no flag set", async () => {
    window.history.replaceState(
      null,
      "",
      "/auth/google/callback#token=abc.def.ghi",
    );

    render(<GoogleCallbackPage />);

    await waitFor(() => {
      expect(setAccessTokenMock).toHaveBeenCalledWith("abc.def.ghi");
    });
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
    expect(window.sessionStorage.getItem(SSO_DISCLOSURE_PENDING_KEY)).toBeNull();
  });

  it("clears the URL fragment after parsing so the token does not persist in history", async () => {
    window.history.replaceState(
      null,
      "",
      "/auth/google/callback#token=abc.def.ghi&created_user=true",
    );

    render(<GoogleCallbackPage />);

    await waitFor(() => {
      expect(setAccessTokenMock).toHaveBeenCalled();
    });
    // After the effect runs, the fragment is gone.
    expect(window.location.hash).toBe("");
  });

  it("missing token routes to /login and does not set the disclosure flag", async () => {
    window.history.replaceState(null, "", "/auth/google/callback");

    render(<GoogleCallbackPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/login");
    });
    expect(setAccessTokenMock).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(SSO_DISCLOSURE_PENDING_KEY)).toBeNull();
  });
});
