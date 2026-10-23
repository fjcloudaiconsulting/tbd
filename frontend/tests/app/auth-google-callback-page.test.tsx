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
 *
 * FE-02: the fragment is accepted only with the nonce this tab stored when
 * it started the sign-in (`tbd.google-sso-nonce` in sessionStorage).
 */
import { render, waitFor } from "@testing-library/react";

import GoogleCallbackPage, {
  SSO_DISCLOSURE_PENDING_KEY,
} from "@/app/auth/google/callback/page";
import { GOOGLE_SSO_NONCE_KEY } from "@/lib/google-sso";

const NONCE = "tab-nonce-0123456789";

/** The state a tab is in after it started a Google sign-in. */
function startedSignIn() {
  window.sessionStorage.setItem(GOOGLE_SSO_NONCE_KEY, NONCE);
}

const setAccessTokenMock = vi.fn();
const refreshMeMock = vi.fn(async () => {});
const replaceMock = vi.fn();

vi.mock("@/lib/api", () => ({
  setAccessToken: vi.fn(),
  signInWithAccessToken: (...args: unknown[]) => setAccessTokenMock(...args),
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
    startedSignIn();
    window.history.replaceState(
      null,
      "",
      `/auth/google/callback#token=abc.def.ghi&created_user=true&nonce=${NONCE}`,
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
    startedSignIn();
    window.history.replaceState(
      null,
      "",
      `/auth/google/callback#token=abc.def.ghi&nonce=${NONCE}`,
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
    startedSignIn();
    window.history.replaceState(
      null,
      "",
      `/auth/google/callback#token=abc.def.ghi&created_user=true&nonce=${NONCE}`,
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

  describe("FE-02: only a sign-in this tab started is accepted", () => {
    it("refuses a planted token when this tab started no sign-in", async () => {
      window.history.replaceState(null, "", `/auth/google/callback#token=planted.jwt.x&nonce=${NONCE}`);

      render(<GoogleCallbackPage />);

      await waitFor(() => {
        expect(replaceMock).toHaveBeenCalledWith("/login?sso_error=state");
      });
      expect(setAccessTokenMock).not.toHaveBeenCalled();
      expect(refreshMeMock).not.toHaveBeenCalled();
      expect(window.location.hash).toBe("");
    });

    it("refuses a token without the tab's nonce, and spends the nonce", async () => {
      startedSignIn();
      window.history.replaceState(null, "", "/auth/google/callback#token=planted.jwt.x&nonce=other-nonce-value");

      render(<GoogleCallbackPage />);

      await waitFor(() => {
        expect(replaceMock).toHaveBeenCalledWith("/login?sso_error=state");
      });
      expect(setAccessTokenMock).not.toHaveBeenCalled();
      expect(window.sessionStorage.getItem(GOOGLE_SSO_NONCE_KEY)).toBeNull();
    });

    it("refuses a token with no nonce at all", async () => {
      startedSignIn();
      window.history.replaceState(null, "", "/auth/google/callback#token=planted.jwt.x");

      render(<GoogleCallbackPage />);

      await waitFor(() => {
        expect(replaceMock).toHaveBeenCalledWith("/login?sso_error=state");
      });
      expect(setAccessTokenMock).not.toHaveBeenCalled();
    });
  });
});
