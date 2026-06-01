import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import ReportsListPage from "@/app/reports/page";
import * as reportsApi from "@/lib/reports/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/reports/api", () => ({
  listReports: vi.fn(),
  listTemplates: vi.fn(),
  createReport: vi.fn(),
  createFromTemplate: vi.fn(),
  deleteReport: vi.fn(),
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => (
      <>{children}</>
    ),
  };
});

const pushMock = vi.fn();
const replaceMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  usePathname: () => "/reports",
}));

const BASE_USER = {
  id: 1,
  username: "alice",
  email: "alice@example.com",
  first_name: "Alice",
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

function mockUser(featureReportsV2 = true) {
  vi.mocked(useAuth).mockReturnValue({
    user: BASE_USER as never,
    loading: false,
    needsSetup: false,
    featureReportsV2,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

describe("ReportsListPage", () => {
  const listMock = vi.mocked(reportsApi.listReports);
  const listTemplatesMock = vi.mocked(reportsApi.listTemplates);
  const createMock = vi.mocked(reportsApi.createReport);
  const deleteMock = vi.mocked(reportsApi.deleteReport);

  beforeEach(() => {
    listMock.mockReset();
    listTemplatesMock.mockReset();
    createMock.mockReset();
    deleteMock.mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
    // Templates load independently of the reports list; default to an
    // empty set so these list-focused tests don't trip on undefined.
    listTemplatesMock.mockResolvedValue([]);
  });

  it("renders inside AppShell so users see the sidebar/nav frame", async () => {
    mockUser(true);
    listMock.mockResolvedValue([]);

    render(<ReportsListPage />);

    await screen.findByTestId("reports-empty-state");
    expect(screen.getByTestId("app-shell")).toBeInTheDocument();
  });

  it("renders the list of reports from the API", async () => {
    mockUser(true);
    listMock.mockResolvedValue([
      {
        id: 10,
        owner_user_id: 1,
        org_id: 1,
        visibility: "private",
        name: "Monthly review",
        description: "January numbers",
        layout_json: {},
        canvas_filters_json: {},
        schema_version: 1,
        created_at: "2026-05-21T10:00:00",
        updated_at: "2026-05-22T10:00:00",
      },
    ]);

    render(<ReportsListPage />);

    await screen.findByText("Monthly review");
    expect(screen.getByText("January numbers")).toBeInTheDocument();
    expect(screen.getByTestId("report-row-10")).toBeInTheDocument();
  });

  it("renders the empty state when the user has no reports yet", async () => {
    mockUser(true);
    listMock.mockResolvedValue([]);

    render(<ReportsListPage />);

    await waitFor(() =>
      expect(screen.getByTestId("reports-empty-state")).toBeInTheDocument(),
    );
    expect(screen.getByText(/No reports yet/i)).toBeInTheDocument();
  });

  it("creates a new report and navigates to its editor on 'New report'", async () => {
    mockUser(true);
    listMock.mockResolvedValue([]);
    createMock.mockResolvedValue({
      id: 42,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "Untitled report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });

    render(<ReportsListPage />);

    await screen.findByTestId("reports-empty-state");
    fireEvent.click(screen.getByRole("button", { name: /new report/i }));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/reports/42"));
  });

  it("seeds a starter bar widget and a date range on 'New report'", async () => {
    mockUser(true);
    listMock.mockResolvedValue([]);
    createMock.mockResolvedValue({
      id: 42,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "Untitled report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });

    render(<ReportsListPage />);

    await screen.findByTestId("reports-empty-state");
    fireEvent.click(screen.getByRole("button", { name: /new report/i }));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    const payload = createMock.mock.calls[0][0];

    // Exactly one bar widget seeded so the new report renders data
    // immediately instead of a blank void.
    expect(payload.layout_json).toBeTruthy();
    const layout = payload.layout_json as { version: number; widgets: unknown[] };
    expect(layout.widgets).toHaveLength(1);
    const widget = layout.widgets[0] as { type: string; config: { measure: unknown } };
    expect(widget.type).toBe("bar");
    // Single-measure bar uses config.measure (not measures[]).
    expect(widget.config.measure).toEqual({ agg: "sum", field: "amount" });

    // Canvas date range is set (this-month window) so the widget shows
    // current data the moment the report opens.
    const filters = payload.canvas_filters_json as {
      date_range?: { start?: string; end?: string };
    };
    expect(filters.date_range).toBeTruthy();
    expect(filters.date_range?.start).toBeTruthy();
    expect(filters.date_range?.end).toBeTruthy();
  });

  it("deletes a report card via confirm and removes it from the list", async () => {
    mockUser(true);
    listMock.mockResolvedValue([
      {
        id: 10,
        owner_user_id: 1,
        org_id: 1,
        visibility: "private",
        name: "Monthly review",
        description: null,
        layout_json: {},
        canvas_filters_json: {},
        schema_version: 1,
        created_at: "2026-05-21T10:00:00",
        updated_at: "2026-05-22T10:00:00",
      },
    ]);
    deleteMock.mockResolvedValue(undefined);

    render(<ReportsListPage />);

    await screen.findByText("Monthly review");
    fireEvent.click(screen.getByTestId("report-delete-10"));
    // Confirm modal → Delete.
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith(10));
    await waitFor(() =>
      expect(screen.queryByText("Monthly review")).toBeNull(),
    );
  });

  it("redirects to /dashboard when feature_reports_v2 is false", async () => {
    mockUser(false);
    listMock.mockResolvedValue([]);

    render(<ReportsListPage />);

    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith("/dashboard"),
    );
    // Should not have queried the API at all.
    expect(listMock).not.toHaveBeenCalled();
  });
});
