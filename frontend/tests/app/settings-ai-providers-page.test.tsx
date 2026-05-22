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
    vi.mocked(apiFetch).mockResolvedValueOnce([fixtureCredential]);

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
    vi.mocked(apiFetch).mockResolvedValueOnce([]);

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
    vi.mocked(apiFetch)
      .mockResolvedValueOnce([]) // initial list
      .mockResolvedValueOnce({ ...fixtureCredential }) // POST create
      .mockResolvedValueOnce([fixtureCredential]); // re-list after create

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
    await waitFor(() =>
      expect(screen.getByText(/prod/)).toBeInTheDocument(),
    );
  });

  it("keeps the modal open and displays the error on a validation failure", async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce([]) // initial list
      .mockRejectedValueOnce(
        new Error("credential_validation_failed: Unauthorized"),
      ); // POST create

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
});
