import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import NotificationsPage from "@/app/settings/notifications/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import type { NotificationPreferences } from "@/lib/types";

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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/settings/notifications",
  useSearchParams: () => new URLSearchParams(),
}));

function makePrefs(
  overrides: Partial<NotificationPreferences> = {},
): NotificationPreferences {
  return {
    email_security: true,
    email_account: true,
    email_org_admin: true,
    email_org_activity: false,
    in_app_security: true,
    in_app_account: true,
    in_app_org_admin: true,
    in_app_org_activity: false,
    ...overrides,
  };
}

function mockAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: {
      id: 1,
      username: "alice",
      email: "alice@acme.io",
      role: "member",
      org_name: "Acme",
      is_superadmin: false,
    } as never,
    loading: false,
    billingUiEnabled: false,
    refreshMe: vi.fn().mockResolvedValue(undefined),
  } as never);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  mockAuth();
});

describe("Notification preferences settings page", () => {
  it("renders the email toggles from the loaded preferences", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(makePrefs());
    render(<NotificationsPage />);

    expect(
      await screen.findByRole("switch", { name: /account email notifications/i }),
    ).toHaveAttribute("aria-checked", "true");
    expect(
      screen.getByRole("switch", {
        name: /organization activity email notifications/i,
      }),
    ).toHaveAttribute("aria-checked", "false");
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/notifications/preferences");
  });

  it("keeps the security toggle on and disabled", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(makePrefs());
    render(<NotificationsPage />);

    const security = await screen.findByRole("switch", {
      name: /security email notifications/i,
    });
    expect(security).toHaveAttribute("aria-checked", "true");
    expect(security).toBeDisabled();
  });

  it("clicking the locked security toggle is a no-op and never PUTs email_security: false", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(makePrefs());
    render(<NotificationsPage />);

    const security = await screen.findByRole("switch", {
      name: /security email notifications/i,
    });
    expect(security).toHaveAttribute("aria-checked", "true");

    // The user tries to switch security off; the locked toggle must ignore it.
    fireEvent.click(security);
    expect(security).toHaveAttribute("aria-checked", "true");

    // Saving afterwards keeps email_security on; no PUT ever carries false.
    vi.mocked(apiFetch).mockResolvedValueOnce(makePrefs());
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() =>
      expect(apiFetch).toHaveBeenLastCalledWith(
        "/api/v1/notifications/preferences",
        expect.objectContaining({ method: "PUT" }),
      ),
    );
    const [, opts] = vi.mocked(apiFetch).mock.calls.at(-1)!;
    const sent = JSON.parse((opts as { body: string }).body);
    expect(sent.email_security).toBe(true);

    // Belt and suspenders: no PUT call anywhere carried email_security: false.
    for (const [, callOpts] of vi.mocked(apiFetch).mock.calls) {
      const body = (callOpts as { body?: string } | undefined)?.body;
      if (body) expect(JSON.parse(body).email_security).not.toBe(false);
    }
  });

  it("toggles a category and PUTs the full preference shape", async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce(makePrefs())
      .mockResolvedValueOnce(makePrefs({ email_org_activity: true }));

    render(<NotificationsPage />);

    const orgActivity = await screen.findByRole("switch", {
      name: /organization activity email notifications/i,
    });
    fireEvent.click(orgActivity);
    expect(orgActivity).toHaveAttribute("aria-checked", "true");

    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() =>
      expect(apiFetch).toHaveBeenLastCalledWith(
        "/api/v1/notifications/preferences",
        expect.objectContaining({ method: "PUT" }),
      ),
    );
    const [, opts] = vi.mocked(apiFetch).mock.calls.at(-1)!;
    const sent = JSON.parse((opts as { body: string }).body);
    // In-app channel fields ride along untouched; only the email side changed.
    expect(sent).toMatchObject({
      email_org_activity: true,
      in_app_security: true,
      in_app_org_activity: false,
    });
    expect(
      await screen.findByText(/notification preferences saved/i),
    ).toBeInTheDocument();
  });

  it("shows an error when loading preferences fails", async () => {
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error("nope"));
    render(<NotificationsPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/nope/i);
  });
});
