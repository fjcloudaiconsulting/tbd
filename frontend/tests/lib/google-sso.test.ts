import { apiFetch } from "@/lib/api";
import { GOOGLE_SSO_NONCE_KEY, startGoogleSignIn, takeGoogleSsoNonce } from "@/lib/google-sso";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

describe("google-sso (FE-02)", () => {
  let savedLocation: Location;
  const hrefSetter = vi.fn();

  beforeAll(() => {
    savedLocation = window.location;
  });

  beforeEach(() => {
    sessionStorage.clear();
    hrefSetter.mockReset();
    vi.mocked(apiFetch).mockReset();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...savedLocation,
        get href() {
          return savedLocation.href;
        },
        set href(value: string) {
          hrefSetter(value);
        },
      },
    });
  });

  afterAll(() => {
    Object.defineProperty(window, "location", { configurable: true, value: savedLocation });
  });

  it("stores the nonce from /auth/google before leaving for Google", async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      redirect_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1",
      nonce: "server-nonce-0123456789",
    } as never);
    await startGoogleSignIn();
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/auth/google");
    expect(sessionStorage.getItem(GOOGLE_SSO_NONCE_KEY)).toBe("server-nonce-0123456789");
    expect(hrefSetter).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/v2/auth?x=1");
  });

  it("drops a stale nonce when the response carries none", async () => {
    sessionStorage.setItem(GOOGLE_SSO_NONCE_KEY, "stale");
    vi.mocked(apiFetch).mockResolvedValue({ redirect_url: "https://accounts.google.com/x" } as never);
    await startGoogleSignIn();
    expect(sessionStorage.getItem(GOOGLE_SSO_NONCE_KEY)).toBeNull();
  });

  it("takes the nonce once", () => {
    sessionStorage.setItem(GOOGLE_SSO_NONCE_KEY, "n");
    expect(takeGoogleSsoNonce()).toBe("n");
    expect(takeGoogleSsoNonce()).toBeNull();
  });
});
