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
import { ApiResponseError, apiFetch } from "@/lib/api";
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import { startOpenRouterConnect } from "@/lib/openrouter-connect";
import type { AIStatus, User } from "@/lib/types";

vi.mock("@/lib/hooks/use-ai-status", () => ({ useAiStatus: vi.fn() }));
vi.mock("@/lib/openrouter-connect", () => ({ startOpenRouterConnect: vi.fn() }));

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
// GET /api/v1/settings/ai-providers now returns a ListEnvelope and the
// credentials table appends sort + pagination query params, so the list GET
// matches by path prefix. POST stays an exact endsWith match.
const isCredentialsListGet = (url: string): boolean =>
  url.includes("/api/v1/settings/ai-providers?");
const credentialsEnvelope = (items: unknown[]) => ({
  items,
  total: items.length,
  limit: 25,
  offset: 0,
});

function mockAuxiliaryEndpoints(credentials: unknown[]) {
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (isCredentialsListGet(url)) {
      return credentialsEnvelope(credentials) as never;
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
        if (isCredentialsListGet(url)) {
          return credentialsEnvelope(creds) as never;
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
        if (isCredentialsListGet(url)) {
          return credentialsEnvelope([]) as never;
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

  const modelList = {
    default: "gpt-4o-mini",
    replies_period: null,
    models: [
      { id: "gpt-4o-mini", label: "GPT-4o mini", hint: null, tier: "budget", replies_left: null },
      { id: "gpt-4o", label: null, hint: null, tier: "frontier", replies_left: null },
    ],
  };

  function mockRouting(opts: {
    credentials?: unknown[];
    routing?: unknown;
    models?: unknown;
    onCall?: (url: string, init?: RequestInit) => void;
    putError?: unknown;
  }) {
    vi.mocked(apiFetch).mockImplementation(
      async (url: string, init?: RequestInit) => {
        opts.onCall?.(url, init);
        if (isCredentialsListGet(url)) {
          return credentialsEnvelope(opts.credentials ?? [fixtureCredential]) as never;
        }
        if (url.endsWith("/options")) {
          return {
            providers: [{ key: "openai", label: "OpenAI", availability: "available" }],
            ai_native_enabled: false,
          } as never;
        }
        if (url.includes("/models")) return (opts.models ?? modelList) as never;
        if (init?.method === "PUT" && opts.putError) throw opts.putError;
        if (url.endsWith("/routing")) {
          return (opts.routing ?? { default: null, features: [] }) as never;
        }
        if (url.endsWith("/caps")) return { default: null, features: [] } as never;
        return undefined as never;
      },
    );
  }

  const routingReady = () =>
    waitFor(() => expect(screen.getByTestId("routing-section")).toBeInTheDocument());

  it("PUTs the default routing payload on save", async () => {
    const calls: { url: string; body: string | null; method: string }[] = [];
    mockRouting({
      onCall: (url, init) =>
        calls.push({ url, body: (init?.body as string) ?? null, method: init?.method ?? "GET" }),
    });

    render(<AiProvidersPage />);
    await routingReady();

    fireEvent.change(screen.getByLabelText(/^Credential$/i), {
      target: { value: "42" },
    });
    // Picking the credential preselects its default model.
    await waitFor(() =>
      expect(screen.getByLabelText(/^Model$/i)).toHaveValue("gpt-4o-mini"),
    );
    // Pick a different listed model from the picker.
    fireEvent.focus(screen.getByLabelText(/^Model$/i));
    fireEvent.click(screen.getByRole("option", { name: /^gpt-4o\s*Frontier/ }));
    fireEvent.click(
      screen.getByRole("button", { name: /Save default/i }),
    );

    await waitFor(() => {
      expect(
        calls.find(
          (c) =>
            c.method === "PUT" &&
            c.url.endsWith("/routing/default") &&
            JSON.parse(c.body ?? "{}").model === "gpt-4o" &&
            JSON.parse(c.body ?? "{}").credential_id === 42,
        ),
      ).toBeTruthy();
    });
  });

  it("P7 default row lists models without a feature, feature rows pass theirs", async () => {
    const urls: string[] = [];
    mockRouting({
      routing: {
        default: { org_id: 1, credential_id: 42, model: "gpt-4o" },
        features: [{ org_id: 1, feature_name: "smart_plan", credential_id: 42, model: "gpt-4o" }],
      },
      onCall: (url) => urls.push(url),
    });
    render(<AiProvidersPage />);
    await routingReady();
    await waitFor(() =>
      expect(urls).toContain("/api/v1/settings/ai-providers/42/models"),
    );
    await waitFor(() =>
      expect(urls).toContain("/api/v1/settings/ai-providers/42/models?feature=smart_plan"),
    );
    // Rows with no credential fetch nothing.
    expect(urls.filter((u) => u.includes("feature=")).length).toBe(1);
  });

  it("P5 a stale saved default keeps its id, warns, disables Save and sends no PUT", async () => {
    const puts: string[] = [];
    mockRouting({
      routing: { default: { org_id: 1, credential_id: 42, model: "gpt-3.5-retired" }, features: [] },
      onCall: (url, init) => { if (init?.method === "PUT") puts.push(url); },
    });
    render(<AiProvidersPage />);
    await routingReady();
    expect(await screen.findByText(/No longer in this key's list/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Model$/i)).toHaveValue("gpt-3.5-retired");
    const save = screen.getByRole("button", { name: /Save default/i });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(puts).toEqual([]);
  });

  it("P14 a stale per-feature override has Save disabled and sends no PUT", async () => {
    const puts: string[] = [];
    mockRouting({
      routing: {
        default: null,
        features: [{ org_id: 1, feature_name: "smart_plan", credential_id: 42, model: "retired-model" }],
      },
      onCall: (url, init) => { if (init?.method === "PUT") puts.push(url); },
    });
    render(<AiProvidersPage />);
    await routingReady();
    const row = screen.getByRole("combobox", { name: "Smart plan model" });
    await waitFor(() => expect(row).toHaveValue("retired-model"));
    await screen.findByText(/No longer in this key's list/);
    const save = within(row.closest("tr") as HTMLElement).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(puts).toEqual([]);
  });

  it("refreshing models refetches the credentials without rewriting a stale saved model", async () => {
    let credGets = 0;
    mockRouting({
      routing: { default: { org_id: 1, credential_id: 42, model: "gone" }, features: [] },
      onCall: (url) => { if (isCredentialsListGet(url)) credGets++; },
    });
    render(<AiProvidersPage />);
    await routingReady();
    const section = screen.getByTestId("routing-section");
    await screen.findByText(/No longer in this key's list/);
    const before = credGets;
    fireEvent.click(screen.getAllByRole("button", { name: "Refresh models" })[0]);
    await waitFor(() => expect(credGets).toBe(before + 1));
    await waitFor(() => expect(screen.getByLabelText(/^Model$/i)).not.toBeDisabled());
    expect(screen.getByTestId("routing-section")).toBe(section);
    expect(screen.getByLabelText(/^Model$/i)).toHaveValue("gone");
  });

  it("an unknown credential hides Refresh and is not called Included AI", async () => {
    mockRouting({
      routing: { default: { org_id: 1, credential_id: 99, model: "x" }, features: [] },
      models: { default: null, replies_period: null, models: [] },
    });
    render(<AiProvidersPage />);
    await routingReady();
    expect(await screen.findByText("This key has no model that can do this.")).toBeInTheDocument();
    expect(screen.queryByText(/Included AI has no model/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Refresh models/ })).not.toBeInTheDocument();
  });

  it("P10 shows the server message when a save is refused with model_not_listed", async () => {
    mockRouting({
      routing: { default: { org_id: 1, credential_id: 42, model: "gpt-4o" }, features: [] },
      putError: new ApiResponseError(400, "That model is not in this key's list.", "model_not_listed"),
    });
    render(<AiProvidersPage />);
    await routingReady();
    const save = screen.getByRole("button", { name: /Save default/i });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    expect(await screen.findByText("That model is not in this key's list.")).toBeInTheDocument();
  });

  it("P8 offers Refresh models for an own key and not for a native one", async () => {
    mockRouting({
      credentials: [fixtureCredential, { ...fixtureCredential, id: 43, provider: "openai_compatible", platform_provider: "openrouter" }],
      routing: { default: { org_id: 1, credential_id: 42, model: "gpt-4o" }, features: [] },
    });
    render(<AiProvidersPage />);
    await routingReady();
    expect(await screen.findAllByRole("button", { name: "Refresh models" })).toHaveLength(1);
    fireEvent.change(screen.getByLabelText(/^Credential$/i), { target: { value: "43" } });
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Refresh models" })).not.toBeInTheDocument(),
    );
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

  it("posts an openai_compatible credential with the preset base URL", async () => {
    const posted: unknown[] = [];
    vi.mocked(apiFetch).mockImplementation(
      async (url: string, init?: RequestInit) => {
        if (
          url.endsWith("/api/v1/settings/ai-providers") &&
          init?.method === "POST"
        ) {
          posted.push(JSON.parse(String(init.body)));
          return fixtureCredential as never;
        }
        if (isCredentialsListGet(url)) {
          return credentialsEnvelope([]) as never;
        }
        if (url.endsWith("/options")) {
          return {
            providers: [
              { key: "openai", label: "OpenAI", availability: "available" },
              { key: "ollama", label: "Ollama", availability: "available" },
              {
                key: "openai_compatible",
                label: "OpenAI-compatible",
                availability: "available",
              },
            ],
            presets: [
              {
                key: "openrouter",
                label: "OpenRouter",
                provider: "openai_compatible",
                base_url: "https://openrouter.ai/api/v1",
              },
            ],
            ai_native_enabled: false,
          } as never;
        }
        if (url.endsWith("/routing") || url.endsWith("/caps")) {
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
    const dialog = screen.getByRole("dialog", { name: /Add AI credential/i });
    const select = within(dialog).getByLabelText(
      /^Provider$/i,
    ) as HTMLSelectElement;
    await waitFor(() =>
      expect(
        Array.from(select.options).some((o) => o.value === "openrouter"),
      ).toBe(true),
    );

    const baseUrlInput = () =>
      within(dialog).getByLabelText(/^Base URL$/i) as HTMLInputElement;

    // A typed URL survives a switch between plain providers.
    fireEvent.change(select, { target: { value: "openai_compatible" } });
    fireEvent.change(baseUrlInput(), {
      target: { value: "https://my.host/v1" },
    });
    fireEvent.change(select, { target: { value: "ollama" } });
    expect(baseUrlInput().value).toBe("https://my.host/v1");

    // Picking a preset takes its URL, even over a typed one.
    fireEvent.change(select, { target: { value: "openrouter" } });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Paste an API key instead" }),
    );
    expect(baseUrlInput().value).toBe("https://openrouter.ai/api/v1");

    // Leaving a preset for another provider clears its untouched URL.
    fireEvent.change(select, { target: { value: "ollama" } });
    expect(baseUrlInput().value).toBe("");

    fireEvent.change(select, { target: { value: "openrouter" } });
    expect(select.value).toBe("openrouter");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Paste an API key instead" }),
    );
    expect(
      (within(dialog).getByLabelText(/^Base URL$/i) as HTMLInputElement).value,
    ).toBe("https://openrouter.ai/api/v1");
    fireEvent.change(within(dialog).getByLabelText(/^API key$/i), {
      target: { value: "sk-or-test-key-abcd" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^Add credential$/i }),
    );

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({
      provider: "openai_compatible",
      api_key: "sk-or-test-key-abcd",
      base_url: "https://openrouter.ai/api/v1",
    });
  });

  describe("OpenRouter connect (TBD-632)", () => {
    const OPENROUTER = "https://openrouter.ai/api/v1";

    function mockWithPreset(credentials: unknown[]) {
      vi.mocked(apiFetch).mockImplementation(async (url: string) => {
        if (isCredentialsListGet(url)) return credentialsEnvelope(credentials) as never;
        if (url.endsWith("/options")) {
          return {
            providers: [{ key: "openai", label: "OpenAI", availability: "available" }],
            presets: [
              {
                key: "openrouter",
                label: "OpenRouter",
                provider: "openai_compatible",
                base_url: OPENROUTER,
              },
            ],
            ai_native_enabled: false,
          } as never;
        }
        if (url.endsWith("/routing") || url.endsWith("/caps")) {
          return { default: null, features: [] } as never;
        }
        return undefined as never;
      });
    }

    async function openModalOnOpenRouter() {
      render(<AiProvidersPage />);
      fireEvent.click(await screen.findByRole("button", { name: /Add credential/i }));
      const dialog = screen.getByRole("dialog", { name: /Add AI credential/i });
      const select = within(dialog).getByLabelText(/^Provider$/i) as HTMLSelectElement;
      await waitFor(() =>
        expect(Array.from(select.options).some((o) => o.value === "openrouter")).toBe(true),
      );
      fireEvent.change(select, { target: { value: "openrouter" } });
      return dialog;
    }

    beforeEach(() => {
      vi.mocked(startOpenRouterConnect).mockReset();
    });

    it("offers Connect instead of a key field, and starts the flow on submit", async () => {
      mockWithPreset([]);
      vi.mocked(startOpenRouterConnect).mockReturnValue(new Promise(() => {}));
      const dialog = await openModalOnOpenRouter();

      expect(within(dialog).queryByLabelText(/^API key$/i)).not.toBeInTheDocument();
      fireEvent.click(within(dialog).getByRole("button", { name: "Connect with OpenRouter" }));

      await waitFor(() => expect(startOpenRouterConnect).toHaveBeenCalledTimes(1));
      expect(within(dialog).getByRole("button", { name: "Opening OpenRouter..." })).toBeDisabled();
      expect(
        vi.mocked(apiFetch).mock.calls.some(
          ([url, init]) => url === "/api/v1/settings/ai-providers" && init?.method === "POST",
        ),
      ).toBe(false);
    });

    it("toggles between connecting and pasting, and reopens OpenRouter in connect mode", async () => {
      mockWithPreset([]);
      const dialog = await openModalOnOpenRouter();
      const select = within(dialog).getByLabelText(/^Provider$/i);

      fireEvent.click(within(dialog).getByRole("button", { name: "Paste an API key instead" }));
      expect(within(dialog).getByLabelText(/^API key$/i)).toBeInTheDocument();
      fireEvent.click(within(dialog).getByRole("button", { name: "Connect with OpenRouter instead" }));
      expect(within(dialog).queryByLabelText(/^API key$/i)).not.toBeInTheDocument();

      fireEvent.click(within(dialog).getByRole("button", { name: "Paste an API key instead" }));
      fireEvent.change(select, { target: { value: "openai" } });
      fireEvent.change(select, { target: { value: "openrouter" } });
      expect(within(dialog).getByRole("button", { name: "Connect with OpenRouter" })).toBeInTheDocument();
    });

    it("re-arms Connect when Back restores the page from the browser cache", async () => {
      mockWithPreset([]);
      vi.mocked(startOpenRouterConnect).mockReturnValue(new Promise(() => {}));
      const dialog = await openModalOnOpenRouter();
      fireEvent.click(within(dialog).getByRole("button", { name: "Connect with OpenRouter" }));
      expect(
        await within(dialog).findByRole("button", { name: "Opening OpenRouter..." }),
      ).toBeDisabled();

      window.dispatchEvent(Object.assign(new Event("pageshow"), { persisted: true }));

      expect(
        await within(dialog).findByRole("button", { name: "Connect with OpenRouter" }),
      ).toBeEnabled();
    });

    it("shows a failed start in the modal and lets the user retry", async () => {
      mockWithPreset([]);
      vi.mocked(startOpenRouterConnect).mockRejectedValue(
        new ApiResponseError(409, "Sign in again to connect OpenRouter.", "oauth_session_unavailable"),
      );
      const dialog = await openModalOnOpenRouter();

      fireEvent.click(within(dialog).getByRole("button", { name: "Connect with OpenRouter" }));

      expect(
        await within(dialog).findByText("Sign in again to connect OpenRouter."),
      ).toBeInTheDocument();
      expect(within(dialog).getByRole("button", { name: "Connect with OpenRouter" })).toBeEnabled();
    });

    it("warns that deleting an own OpenRouter key does not revoke it there", async () => {
      const pasted = { ...fixtureCredential, id: 5, provider: "openai_compatible", base_url: `${OPENROUTER}/` };
      // The platform's own OpenRouter key is not the user's to delete at OpenRouter.
      const platform = { ...pasted, id: 6, platform_provider: "openrouter", base_url: OPENROUTER };
      mockWithPreset([pasted, fixtureCredential, platform]);
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
      render(<AiProvidersPage />);
      const table = await screen.findByTestId("credentials-table");
      const [openRouterDelete, openAiDelete, platformDelete] = within(table).getAllByRole(
        "button",
        { name: "Delete" },
      );

      fireEvent.click(openRouterDelete);
      fireEvent.click(openAiDelete);
      fireEvent.click(platformDelete);

      expect(confirmSpy.mock.calls[0][0]).toMatch(
        /It keeps working at OpenRouter until you delete it at openrouter\.ai\/settings\/keys\./,
      );
      expect(confirmSpy.mock.calls[1][0]).toBe(
        "Delete this credential? This action cannot be undone.",
      );
      expect(confirmSpy.mock.calls[2][0]).toBe(
        "Delete this credential? This action cannot be undone.",
      );
      confirmSpy.mockRestore();
    });
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

  describe("plan usage meters (TBD-622)", () => {
    const meter = { used: 1, limit: null, period: "month", resets_at: null };
    const status = (entitled: boolean) => ({
      agent: { entitled, configured: true },
      usage: { "assistant.turns": meter, "mcp.calls": meter, "platform_ai.tokens": meter },
    }) as unknown as AIStatus;

    it("hides the assistant and agent meters when the org is not entitled to ai.agent", async () => {
      mockAuxiliaryEndpoints([fixtureCredential]);
      vi.mocked(useAiStatus).mockReturnValue(status(false));
      render(<AiProvidersPage />);
      expect(await screen.findByText("Platform AI tokens")).toBeInTheDocument();
      expect(screen.queryByText("Assistant messages")).not.toBeInTheDocument();
      expect(screen.queryByText("Connected agent calls")).not.toBeInTheDocument();
    });

    it("shows them when entitled", async () => {
      mockAuxiliaryEndpoints([fixtureCredential]);
      vi.mocked(useAiStatus).mockReturnValue(status(true));
      render(<AiProvidersPage />);
      expect(await screen.findByText("Assistant messages")).toBeInTheDocument();
      expect(screen.getByText("Connected agent calls")).toBeInTheDocument();
    });
  });
});
