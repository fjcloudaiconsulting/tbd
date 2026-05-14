/**
 * /settings/security step-up error banner coverage (Finding 1).
 *
 * The step-up flow initiated from this page encodes `return_to: "security"`
 * in state, so failures redirect back to /settings/security?sso_stepup_error=
 * <code>. Pre-fix the page had no banner UI, so the user saw nothing.
 * These tests pin:
 *
 *   - the banner renders for each error code with the security-context copy
 *   - the retry CTA wires through the existing Verify-with-Google flow,
 *     hitting /api/v1/auth/sso-stepup/initiate with `{ return_to: "security" }`
 *     so a successful retry lands back here, not on /settings
 *   - dismiss/retry strips ?sso_stepup_error= from the URL
 *
 * The Verify-with-Google handler does `window.location.href = data.redirect_url`
 * on success. jsdom emits "Not implemented: navigation" to stderr for that
 * top-level navigation, so we stub `window.location` to a spyable mock
 * before the test runs (Finding 3). Without the stub the test still passes
 * but the stderr noise pollutes CI.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SecurityPage from "@/app/settings/security/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import * as nextNavigation from "next/navigation";

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

const routerReplaceMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: routerReplaceMock }),
  usePathname: () => "/settings/security",
  // Default: no `?sso_stepup_error=` in the URL. Tests override
  // per-render with vi.spyOn().
  useSearchParams: () => new URLSearchParams(),
}));

function makeUser(passwordSet: boolean) {
  return {
    id: 1,
    username: "alice",
    email: "alice@acme.io",
    first_name: null,
    last_name: null,
    phone: null,
    avatar_url: null,
    email_verified: true,
    role: "owner" as const,
    org_id: 1,
    org_name: "Acme",
    billing_cycle_day: 1,
    is_superadmin: false,
    is_active: true,
    mfa_enabled: false,
    password_set: passwordSet,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
  };
}

function mockUser(passwordSet: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser(passwordSet) as never,
    loading: false,
    needsSetup: false,
    login: vi.fn().mockResolvedValue(undefined),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn().mockResolvedValue(undefined),
  } as never);
}

/**
 * Replace `window.location` with a spyable object so the
 * Verify-with-Google handler's `window.location.href = ...` assignment
 * doesn't trip jsdom's "Not implemented: navigation" warning during the
 * retry test (Finding 3). Returns the mock so individual tests can
 * assert on the assigned href.
 */
function stubWindowLocation(): { hrefSetter: ReturnType<typeof vi.fn> } {
  const hrefSetter = vi.fn();
  const original = window.location;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      ...original,
      assign: vi.fn(),
      // The handler does `window.location.href = data.redirect_url`.
      // Intercept that set by defining a getter/setter pair.
      get href() {
        return original.href;
      },
      set href(value: string) {
        hrefSetter(value);
      },
    },
  });
  return { hrefSetter };
}

let savedLocation: Location;

describe("Settings/Security page — SSO step-up error banner (Finding 1)", () => {
  beforeAll(() => {
    savedLocation = window.location;
  });

  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    // Page mounts an admin-only /api/v1/settings fetch in a useEffect.
    // Default to an empty list so the .then() chain has something
    // to consume; individual tests can override per-call.
    vi.mocked(apiFetch).mockResolvedValue([] as never);
    routerReplaceMock.mockReset();
  });

  afterEach(() => {
    // Restore any `vi.spyOn(nextNavigation, "useSearchParams")` rebind
    // so a later test's "no sso_stepup_error" expectation isn't leaked
    // into by the prior test's URLSearchParams override.
    vi.restoreAllMocks();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: savedLocation,
    });
    window.location.hash = "";
  });

  it.each([
    { code: "state", needle: /expired\. Try again to update your password/i },
    { code: "token", needle: /verification didn't complete/i },
    { code: "userinfo", needle: /verification didn't complete/i },
    { code: "unverified", needle: /isn't verified/i },
    { code: "email_mismatch", needle: /doesn't match this profile/i },
    { code: "cancelled", needle: /You cancelled the Google verification/i },
    {
      code: "provider_error",
      needle: /Google returned an error during verification/i,
    },
  ])(
    "renders the banner with security-context copy for ?sso_stepup_error=$code",
    ({ code, needle }) => {
      mockUser(false);
      vi.spyOn(nextNavigation, "useSearchParams").mockReturnValue(
        new URLSearchParams(`sso_stepup_error=${code}`) as never,
      );
      render(<SecurityPage />);
      const banner = screen.getByTestId("sso-stepup-error-banner");
      expect(banner.textContent).toMatch(needle);
      expect(
        screen.getByRole("button", { name: /Try again with Google/i }),
      ).toBeInTheDocument();
    },
  );

  it("falls back to a generic message for an unknown sso_stepup_error code", () => {
    mockUser(false);
    vi.spyOn(nextNavigation, "useSearchParams").mockReturnValue(
      new URLSearchParams("sso_stepup_error=brand_new_code") as never,
    );
    render(<SecurityPage />);
    const banner = screen.getByTestId("sso-stepup-error-banner");
    expect(banner.textContent).toMatch(/didn't complete\. Try again/i);
  });

  it("does not render the banner when the URL has no sso_stepup_error param", () => {
    mockUser(false);
    render(<SecurityPage />);
    expect(screen.queryByTestId("sso-stepup-error-banner")).toBeNull();
  });

  it("retry CTA re-initiates step-up with return_to: 'security' (no jsdom navigation warning)", async () => {
    mockUser(false);
    vi.spyOn(nextNavigation, "useSearchParams").mockReturnValue(
      new URLSearchParams("sso_stepup_error=state") as never,
    );
    stubWindowLocation();
    // The page mounts an admin /api/v1/settings effect plus the retry
    // click hits /api/v1/auth/sso-stepup/initiate. Route by URL so
    // the retry assertion is stable regardless of call order.
    vi.mocked(apiFetch).mockImplementation((url: string) => {
      if (url === "/api/v1/auth/sso-stepup/initiate") {
        return Promise.resolve({
          redirect_url: "https://accounts.google.com/fake",
        }) as never;
      }
      return Promise.resolve([] as never);
    });

    render(<SecurityPage />);
    fireEvent.click(
      screen.getByRole("button", { name: /Try again with Google/i }),
    );

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        "/api/v1/auth/sso-stepup/initiate",
        expect.objectContaining({
          method: "POST",
          // The page sends return_to: "security" so a successful retry
          // lands back here, not on /settings.
          body: JSON.stringify({ return_to: "security" }),
        }),
      );
    });
    // And the URL was stripped of ?sso_stepup_error= via router.replace.
    expect(routerReplaceMock).toHaveBeenCalled();
  });

  it("dismiss strips ?sso_stepup_error= from the URL", () => {
    mockUser(false);
    vi.spyOn(nextNavigation, "useSearchParams").mockReturnValue(
      new URLSearchParams("sso_stepup_error=state") as never,
    );
    render(<SecurityPage />);
    fireEvent.click(screen.getByRole("button", { name: /Dismiss/i }));
    expect(routerReplaceMock).toHaveBeenCalled();
    // After dismiss the banner is gone.
    expect(screen.queryByTestId("sso-stepup-error-banner")).toBeNull();
  });
});
