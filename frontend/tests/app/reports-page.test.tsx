import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ReportsListPage from "@/app/reports/page";
import * as reportsApi from "@/lib/reports/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/reports/api", () => ({
  listReports: vi.fn(),
  listTemplates: vi.fn(),
  createReport: vi.fn(),
  createFromTemplate: vi.fn(),
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

  beforeEach(() => {
    listMock.mockReset();
    listTemplatesMock.mockReset();
    createMock.mockReset();
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
