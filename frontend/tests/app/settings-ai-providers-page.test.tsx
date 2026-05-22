import React from "react";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  within,
} from "@testing-library/react";
import { useRouter } from "next/navigation";

import AiProvidersPage from "@/app/settings/ai-providers/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { User } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});

function adminUser(): User {
  return {
    id: 1,
    username: "admin",
    email: "admin@example.com",
    first_name: "Aida",
    last_name: "Admin",
    phone: null,
    avatar_url: null,
    email_verified: true,
    role: "admin",
    org_id: 1,
    org_name: "Test Org",
    billing_cycle_day: 1,
    is_superadmin: false,
    is_active: true,
    mfa_enabled: false,
    password_set: true,
    allow_manual_balance_adjustment: false,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
  };
}

function mockAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: adminUser(),
    loading: false,
    needsSetup: false,
    billingUiEnabled: true,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

const fixtureCredential = {
  id: 42,
  org_id: 1,
  provider: "openai",
  last_four: "abcd",
  key_fingerprint: "deadbeefcafebabe",
  base_url: null,
  label: "prod",
  discovered_capabilities: ["chat", "embed"],
  discovered_models: ["gpt-4o", "gpt-4o-mini"],
  created_at: "2026-05-22T10:00:00Z",
  updated_at: "2026-05-22T10:00:00Z",
  last_used_at: null,
  last_validated_at: "2026-05-22T10:00:00Z",
  validation_error: null,
};

// The page now hits four endpoints in parallel on every fetchAll: list,
// options, routing, caps. The routing + caps responses are optional
// (the page tolerates them missing), but ``providers`` must be an array
// for the modal picker to render. The factory below mocks the URL the
// caller hit so tests can stay focused on the credentials behavior they
// were written for.
function mockAuxiliaryEndpoints(credentials: unknown[]) {
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.endsWith("/api/v1/settings/ai-providers")) {
      return credentials as never;
    }
    if (url.endsWith("/options")) {
      return {
        providers: [
          { key: "openai", label: "OpenAI", availability: "available" },
          { key: "native", label: "Native", availability: "not_yet_available" },
        ],
        ai_native_enabled: false,
      } as never;
    }
    if (url.endsWith("/routing")) {
      return { default: null, features: [] } as never;
    }
    if (url.endsWith("/caps")) {
      return { default: null, features: [] } as never;
    }
    return undefined as never;
  });
}

describe("AiProvidersPage", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useRouter).mockReturnValue({
      replace: vi.fn(),
      push: vi.fn(),
    } as never);
    mockAuth();
  });

  it("renders the credentials table with the fixture row", async () => {
    mockAuxiliaryEndpoints([fixtureCredential]);

    render(<AiProvidersPage />);

    await waitFor(() =>
      expect(screen.getByTestId("credentials-table")).toBeInTheDocument(),
    );
    const table = screen.getByTestId("credentials-table");
    expect(within(table).getByText("OpenAI")).toBeInTheDocument();
    expect(within(table).getByText("prod")).toBeInTheDocument();
    expect(within(table).getByText("***abcd")).toBeInTheDocument();
    expect(within(table).getByText("2 models")).toBeInTheDocument();
  });

  it("opens the add-credential modal when the button is clicked", async () => {
    mockAuxiliaryEndpoints([]);

    render(<AiProvidersPage />);

    await waitFor(() =>
      expect(
        screen.getByText(/No credentials configured yet/i),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /Add credential/i }));
    expect(
      screen.getByRole("dialog", { name: /Add AI credential/i }),
    ).toBeInTheDocument();
  });

  it("closes the modal and refreshes the list on a successful add", async () => {
    // First mount: zero credentials. After POST + refresh: one row.
    let creds: unknown[] = [];
    vi.mocked(apiFetch).mockImplementation(
      async (url: string, init?: RequestInit) => {
        if (
          url.endsWith("/api/v1/settings/ai-providers") &&
          init?.method === "POST"
        ) {
          creds = [fixtureCredential];
          return fixtureCredential as never;
        }
        if (url.endsWith("/api/v1/settings/ai-providers")) {
          return creds as never;
        }
        if (url.endsWith("/options")) {
          return {
            providers: [
              { key: "openai", label: "OpenAI", availability: "available" },
            ],
            ai_native_enabled: false,
          } as never;
        }
        if (url.endsWith("/routing")) {
          return { default: null, features: [] } as never;
        }
        if (url.endsWith("/caps")) {
          return { default: null, features: [] } as never;
        }
        return undefined as never;
      },
    );

    render(<AiProvidersPage />);

    await waitFor(() =>
      expect(
        screen.getByText(/No credentials configured yet/i),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Add credential/i }));

    fireEvent.change(screen.getByLabelText(/^API key$/i), {
      target: { value: "sk-test-good-key-abcd" },
    });
    const dialog = screen.getByRole("dialog", {
      name: /Add AI credential/i,
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^Add credential$/i }),
    );

    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: /Add AI credential/i }),
      ).not.toBeInTheDocument();
    });
    await waitFor(() => {
      const table = screen.getByTestId("credentials-table");
      expect(within(table).getByText("prod")).toBeInTheDocument();
    });
  });

  it("keeps the modal open and displays the error on a validation failure", async () => {
    vi.mocked(apiFetch).mockImplementation(
      async (url: string, init?: RequestInit) => {
        if (
          url.endsWith("/api/v1/settings/ai-providers") &&
          init?.method === "POST"
        ) {
          throw new Error("credential_validation_failed: Unauthorized");
        }
        if (url.endsWith("/api/v1/settings/ai-providers")) {
          return [] as never;
        }
        if (url.endsWith("/options")) {
          return {
            providers: [
              { key: "openai", label: "OpenAI", availability: "available" },
            ],
            ai_native_enabled: false,
          } as never;
        }
        if (url.endsWith("/routing")) {
          return { default: null, features: [] } as never;
        }
        if (url.endsWith("/caps")) {
          return { default: null, features: [] } as never;
        }
        return undefined as never;
      },
    );

    render(<AiProvidersPage />);

    await waitFor(() =>
      expect(
        screen.getByText(/No credentials configured yet/i),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Add credential/i }));

    fireEvent.change(screen.getByLabelText(/^API key$/i), {
      target: { value: "sk-test-bad-key-abcd" },
    });
    const dialog = screen.getByRole("dialog", {
      name: /Add AI credential/i,
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^Add credential$/i }),
    );

    // Modal still open, error rendered inside.
    await waitFor(() => {
      expect(
        screen.getByRole("dialog", { name: /Add AI credential/i }),
      ).toBeInTheDocument();
      expect(screen.getByText(/Unauthorized/)).toBeInTheDocument();
    });
  });

  it("renders the routing section with default + feature override picker", async () => {
    mockAuxiliaryEndpoints([fixtureCredential]);

    render(<AiProvidersPage />);
    await waitFor(() =>
      expect(screen.getByTestId("routing-section")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("feature-routing-table")).toBeInTheDocument();
    // The closed feature set surfaces in the UI.
    expect(screen.getByText("Categorize transactions")).toBeInTheDocument();
    expect(screen.getByText("Smart forecast")).toBeInTheDocument();
  });

  it("PUTs the default routing payload on save", async () => {
    const calls: { url: string; body: string | null; method: string }[] = [];
    vi.mocked(apiFetch).mockImplementation(
      async (url: string, init?: RequestInit) => {
        calls.push({
          url,
          body: (init?.body as string) ?? null,
          method: init?.method ?? "GET",
        });
        if (url.endsWith("/api/v1/settings/ai-providers")) {
          return [fixtureCredential] as never;
        }
        if (url.endsWith("/options")) {
          return {
            providers: [
              { key: "openai", label: "OpenAI", availability: "available" },
            ],
            ai_native_enabled: false,
          } as never;
        }
        if (url.endsWith("/routing")) {
          return { default: null, features: [] } as never;
        }
        if (url.endsWith("/caps")) {
          return { default: null, features: [] } as never;
        }
        return undefined as never;
      },
    );

    render(<AiProvidersPage />);
    await waitFor(() =>
      expect(screen.getByTestId("routing-section")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText(/^Credential$/i), {
      target: { value: "42" },
    });
    fireEvent.change(screen.getByLabelText(/^Model$/i), {
      target: { value: "gpt-4o-mini" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Save default/i }),
    );

    await waitFor(() => {
      expect(
        calls.find(
          (c) =>
            c.method === "PUT" &&
            c.url.endsWith("/routing/default") &&
            (c.body ?? "").includes("gpt-4o-mini") &&
            (c.body ?? "").includes("42"),
        ),
      ).toBeTruthy();
    });
  });

  it("disables the native option in the provider picker when not_yet_available", async () => {
    mockAuxiliaryEndpoints([]);

    render(<AiProvidersPage />);
    await waitFor(() =>
      expect(
        screen.getByText(/No credentials configured yet/i),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Add credential/i }));

    const dialog = screen.getByRole("dialog", {
      name: /Add AI credential/i,
    });
    const select = within(dialog).getByLabelText(/^Provider$/i) as HTMLSelectElement;
    const nativeOption = Array.from(select.options).find(
      (o) => o.value === "native",
    );
    expect(nativeOption).toBeTruthy();
    expect(nativeOption?.disabled).toBe(true);
    expect(nativeOption?.textContent).toMatch(/coming soon/i);
  });

  it("renders the caps section even when no caps are set", async () => {
    mockAuxiliaryEndpoints([]);

    render(<AiProvidersPage />);
    await waitFor(() =>
      expect(screen.getByTestId("caps-section")).toBeInTheDocument(),
    );
    expect(screen.getByLabelText(/Default soft cap/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Default hard cap/i)).toBeInTheDocument();
  });
});
