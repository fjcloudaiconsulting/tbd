import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { SWRConfig } from "swr";

// Stub the canvas (react-grid-layout) — jsdom can't measure container
// width so the responsive grid silently collapses to width=-1 and
// neither widgets nor drag handles render. The stub keeps the
// render-tree shape (calls ``renderWidget`` for each widget) so
// downstream interactions inside widget shells still work.
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

vi.mock("@/lib/reports/api", () => ({
  getReport: vi.fn(),
  saveLayout: vi.fn(),
  runQuery: vi.fn(),
  deleteReport: vi.fn(),
  resetReport: vi.fn(),
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
// Stable router object — returning a fresh literal every call would
// invalidate the editor's useEffect deps on each render and re-fire
// getReport in a loop that resets layout state under us.
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

function makeParams(id = "10") {
  // The editor page accepts either a Promise (production / Next 15
  // routing) or a plain object (test harness). We pass the plain
  // object so the page doesn't suspend on ``use()``.
  return { id };
}

function renderIsolated(ui: React.ReactElement) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>,
  );
}

describe("ReportEditorPage", () => {
  const getReportMock = vi.mocked(reportsApi.getReport);
  const saveLayoutMock = vi.mocked(reportsApi.saveLayout);
  const runQueryMock = vi.mocked(reportsApi.runQuery);
  const deleteReportMock = vi.mocked(reportsApi.deleteReport);
  const resetReportMock = vi.mocked(reportsApi.resetReport);

  beforeEach(() => {
    getReportMock.mockReset();
    saveLayoutMock.mockReset();
    runQueryMock.mockReset();
    deleteReportMock.mockReset();
    resetReportMock.mockReset();
    runQueryMock.mockResolvedValue({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 1 },
    });
    pushMock.mockReset();
    replaceMock.mockReset();
  });

  it("renders the canvas for an empty report and opens the widget picker", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    // The editor renders inside AppShell so users keep the
    // sidebar/nav frame + shell-level surfaces.
    expect(screen.getByTestId("app-shell")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-empty")).toBeInTheDocument();

    // Add widget opens the picker dialog.
    fireEvent.click(screen.getByTestId("report-editor-add-widget"));
    expect(screen.getByTestId("widget-picker")).toBeInTheDocument();
    expect(
      screen.getByTestId("widget-picker-option-kpi"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("widget-picker-option-bar"),
    ).toBeInTheDocument();
  });

  it("adds a KPI widget through the picker and surfaces it on the canvas", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    fireEvent.click(screen.getByTestId("report-editor-add-widget"));
    fireEvent.click(screen.getByTestId("widget-picker-option-kpi"));

    await waitFor(() =>
      expect(screen.getByTestId("kpi-widget")).toBeInTheDocument(),
    );
    // Picker dismisses after pick.
    expect(screen.queryByTestId("widget-picker")).toBeNull();
    // The newly added widget is selected → ConfigRail mounts.
    await waitFor(() =>
      expect(screen.getByTestId("config-rail")).toBeInTheDocument(),
    );
  });

  it("saves the layout via PATCH when 'Save' is clicked", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });
    saveLayoutMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:01",
    });

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    // Make the report dirty by adding a widget.
    fireEvent.click(screen.getByTestId("report-editor-add-widget"));
    fireEvent.click(screen.getByTestId("widget-picker-option-bar"));

    await waitFor(() =>
      expect(screen.getByTestId("report-editor-dirty")).toBeInTheDocument(),
    );
    // Wait for the bar widget to land so the Save button reflects the
    // dirty layout state on click.
    await waitFor(() =>
      expect(screen.getByTestId("bar-widget")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("report-editor-save"));

    await waitFor(() => expect(saveLayoutMock).toHaveBeenCalledTimes(1));
    // Args: report id, layout, canvas filters.
    const [calledId, calledLayout] = saveLayoutMock.mock.calls[0];
    expect(calledId).toBe(10);
    expect(calledLayout.version).toBe(1);
    expect(calledLayout.widgets).toHaveLength(1);
    expect(calledLayout.widgets[0].type).toBe("bar");
  });

  it("propagates the canvas-wide date filter into widget queries (cascade)", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: {
        version: 1,
        widgets: [
          {
            id: "w_1",
            type: "kpi",
            title: "Total",
            grid: { x: 0, y: 0, w: 3, h: 2 },
            config: {
              dataset: "transactions",
              measure: { agg: "sum", field: "amount" },
              format: "currency",
            },
          },
        ],
      },
      canvas_filters_json: {
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      },
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());
    const lastCall = runQueryMock.mock.calls[runQueryMock.mock.calls.length - 1];
    const ast = lastCall[0];
    // The canvas date range cascaded into the widget's resolved AST.
    const dateFilter = ast.filters.find(
      (f: { field: string }) => f.field === "date",
    );
    expect(dateFilter).toBeDefined();
    expect(dateFilter!.op).toBe("between");
    expect(dateFilter!.value).toEqual(["2026-01-01", "2026-01-31"]);
  });

  it("shows the 'Overrides canvas' pill when a widget overrides a canvas filter", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: {
        version: 1,
        widgets: [
          {
            id: "w_1",
            type: "kpi",
            title: "Total",
            grid: { x: 0, y: 0, w: 3, h: 2 },
            config: {
              dataset: "transactions",
              measure: { agg: "sum", field: "amount" },
              format: "currency",
              filters: {
                date_range: { start: "2026-02-01", end: "2026-02-15" },
              },
            },
          },
        ],
      },
      canvas_filters_json: {
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      },
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    // Click the widget shell to select it; config rail mounts.
    const shell = await screen.findByTestId("kpi-widget");
    fireEvent.click(shell);

    await waitFor(() =>
      expect(screen.getByTestId("config-rail")).toBeInTheDocument(),
    );
    // Override pill appears on the overridden date field.
    expect(screen.getByTestId("override-pill")).toBeInTheDocument();
  });

  it("deletes the report via confirm and navigates back to /reports", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });
    deleteReportMock.mockResolvedValue(undefined);

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    fireEvent.click(screen.getByTestId("report-editor-delete"));
    // Confirm modal appears; clicking its Confirm button fires the
    // delete. Scope to the dialog so we don't match the header trigger.
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteReportMock).toHaveBeenCalledWith(10));
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/reports"));
  });

  it("cancels editing: discards unsaved changes and exits edit mode", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    // Make a dirty change.
    fireEvent.click(screen.getByTestId("report-editor-add-widget"));
    fireEvent.click(screen.getByTestId("widget-picker-option-bar"));
    await waitFor(() =>
      expect(screen.getByTestId("bar-widget")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("report-editor-dirty")).toBeInTheDocument();

    // Cancel discards the unsaved widget and exits edit mode.
    fireEvent.click(screen.getByTestId("report-editor-cancel"));

    await waitFor(() =>
      expect(screen.queryByTestId("bar-widget")).toBeNull(),
    );
    expect(screen.queryByTestId("report-editor-dirty")).toBeNull();
    // Exited edit mode: the toggle now reads "Edit", Add widget is gone.
    expect(screen.queryByTestId("report-editor-add-widget")).toBeNull();
    // No server save was issued by Cancel.
    expect(saveLayoutMock).not.toHaveBeenCalled();
  });

  it("reverts to original: calls resetReport and re-hydrates from the response", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: {
        version: 1,
        widgets: [
          {
            id: "w_kpi",
            type: "kpi",
            title: "Total",
            grid: { x: 0, y: 0, w: 3, h: 2 },
            config: {
              dataset: "transactions",
              measure: { agg: "sum", field: "amount" },
              format: "currency",
            },
          },
        ],
      },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });
    // Reverted server snapshot returns an empty layout (the as-created
    // state had no widgets).
    resetReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "My report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:02",
    });

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    // Widget from the loaded report is present.
    await screen.findByTestId("kpi-widget");

    fireEvent.click(screen.getByTestId("report-editor-revert"));
    // Confirm modal → Revert (scope to the dialog).
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Revert" }));

    await waitFor(() => expect(resetReportMock).toHaveBeenCalledWith(10));
    // Page re-hydrates from the returned (empty) layout.
    await waitFor(() =>
      expect(screen.queryByTestId("kpi-widget")).toBeNull(),
    );
    expect(screen.getByTestId("report-editor-empty")).toBeInTheDocument();
  });

  it("redirects to /dashboard when feature_reports_v2 is false", async () => {
    mockUser(false);
    getReportMock.mockResolvedValue({
      id: 10,
    } as never);

    renderIsolated(<ReportEditorPage params={makeParams()} />);

    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith("/dashboard"),
    );
    expect(getReportMock).not.toHaveBeenCalled();
  });
});
