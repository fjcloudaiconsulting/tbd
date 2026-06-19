import {
  renderWithSWR,
  fireEvent,
  screen,
  waitFor,
} from "../utils/render-with-swr";

// Stub the canvas (react-grid-layout) — jsdom can't measure container
// width so the responsive grid silently collapses. The stub keeps the
// render-tree shape so downstream interactions still work.
vi.mock("@/components/reports/Canvas", () => ({
  default: ({ layout, renderWidget }: {
    layout: { widgets: { id: string }[] };
    renderWidget: (w: { id: string }) => React.ReactNode;
  }) => (
    <div data-testid="reports-canvas">
      {layout.widgets.map((w) => (
        <div key={w.id} data-widget-id={w.id}>
          {renderWidget(w as never)}
        </div>
      ))}
    </div>
  ),
}));

vi.mock("@/lib/reports/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/reports/api")>(
    "@/lib/reports/api",
  );
  return {
    ...actual,
    getReport: vi.fn(),
    saveLayout: vi.fn(),
    runQuery: vi.fn(),
    deleteReport: vi.fn(),
    listVersions: vi.fn(),
    restoreVersion: vi.fn(),
    updateReport: vi.fn(),
    duplicateReport: vi.fn(),
  };
});

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
const stableRouter = { push: pushMock, replace: replaceMock };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/reports/10",
}));

import ReportEditorPage from "@/app/reports/[id]/page";
import { useAuth } from "@/components/auth/AuthProvider";
import * as reportsApi from "@/lib/reports/api";

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

function mockUser(reportsOn = true) {
  vi.mocked(useAuth).mockReturnValue({
    user: BASE_USER as never,
    loading: false,
    needsSetup: false,
    features: { reports: reportsOn, plans: false },
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

function makeParams(id = "10") {
  return { id };
}

const EMPTY_REPORT = {
  id: 10,
  owner_user_id: 1,
  org_id: 1,
  visibility: "private" as const,
  name: "My report",
  description: null,
  layout_json: { version: 1 as const, widgets: [] },
  canvas_filters_json: {},
  schema_version: 1,
  created_at: "2026-05-22T10:00:00",
  updated_at: "2026-05-22T10:00:00",
};

const REPORT_WITH_WIDGET = {
  ...EMPTY_REPORT,
  layout_json: {
    version: 1 as const,
    widgets: [
      {
        id: "w_kpi",
        type: "kpi" as const,
        title: "Total",
        grid: { x: 0, y: 0, w: 3, h: 2 },
        config: {
          dataset: "transactions" as const,
          measure: { agg: "sum" as const, field: "amount" as const },
          format: "currency" as const,
        },
      },
    ],
  },
};

describe("ReportEditorPage — inline title edit", () => {
  const getReportMock = vi.mocked(reportsApi.getReport);
  const runQueryMock = vi.mocked(reportsApi.runQuery);
  const updateReportMock = vi.mocked(reportsApi.updateReport);

  beforeEach(() => {
    getReportMock.mockReset();
    runQueryMock.mockReset();
    updateReportMock.mockReset();
    runQueryMock.mockResolvedValue({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 1 },
    });
    pushMock.mockReset();
    replaceMock.mockReset();
    // @ts-expect-error -- clear any matchMedia stub a prior test installed
    delete window.matchMedia;
  });

  it("renders an editable title input in edit mode and PATCHes the new name on commit", async () => {
    mockUser(true);
    // Empty report → owner opens in edit mode automatically.
    getReportMock.mockResolvedValue(EMPTY_REPORT as never);
    updateReportMock.mockResolvedValue({
      ...EMPTY_REPORT,
      name: "Renamed report",
      updated_at: "2026-05-22T11:00:00",
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    const input = screen.getByLabelText("Report title") as HTMLInputElement;
    expect(input.value).toBe("My report");

    fireEvent.change(input, { target: { value: "Renamed report" } });
    fireEvent.blur(input);

    await waitFor(() =>
      expect(updateReportMock).toHaveBeenCalledWith(10, {
        name: "Renamed report",
      }),
    );
    // The header reflects the persisted name.
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Report title") as HTMLInputElement).value,
      ).toBe("Renamed report"),
    );
  });

  it("commits the rename on Enter as well as blur", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(EMPTY_REPORT as never);
    updateReportMock.mockResolvedValue({
      ...EMPTY_REPORT,
      name: "Via enter",
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    const input = screen.getByLabelText("Report title");
    fireEvent.change(input, { target: { value: "Via enter" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() =>
      expect(updateReportMock).toHaveBeenCalledWith(10, { name: "Via enter" }),
    );
  });

  it("does not PATCH when the name is unchanged", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(EMPTY_REPORT as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    const input = screen.getByLabelText("Report title");
    fireEvent.blur(input);

    expect(updateReportMock).not.toHaveBeenCalled();
  });

  it("reverts to the current name and does not PATCH when committed empty", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(EMPTY_REPORT as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    const input = screen.getByLabelText("Report title") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.blur(input);

    expect(updateReportMock).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Report title") as HTMLInputElement).value,
      ).toBe("My report"),
    );
  });

  it("renders the title as static text (no input) in view mode", async () => {
    mockUser(true);
    // Report with a widget → owner opens in VIEW mode.
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    // View mode: no editable title input.
    expect(screen.queryByLabelText("Report title")).toBeNull();
    // The name still shows as static text.
    expect(screen.getByText("My report")).toBeInTheDocument();
  });
});
