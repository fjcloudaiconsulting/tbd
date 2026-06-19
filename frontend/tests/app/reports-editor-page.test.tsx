import {
  renderWithSWR,
  fireEvent,
  screen,
  waitFor,
  within,
} from "../utils/render-with-swr";

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
  listVersions: vi.fn(),
  restoreVersion: vi.fn(),
  updateReport: vi.fn(),
  duplicateReport: vi.fn(),
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

import ReportEditorPage, {
  orderWidgetsForStack,
} from "@/app/reports/[id]/page";
import { useAuth } from "@/components/auth/AuthProvider";
import * as reportsApi from "@/lib/reports/api";
import type { Widget } from "@/lib/reports/types";

// Install a ``matchMedia`` stub that reports the given small-screen
// state for the ``max-width`` query the page uses. jsdom ships no
// matchMedia, so absent this the page treats every test as desktop.
function mockMatchMedia(isSmall: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: /max-width/.test(query) ? isSmall : false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

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
  // The editor page accepts either a Promise (production / Next 15
  // routing) or a plain object (test harness). We pass the plain
  // object so the page doesn't suspend on ``use()``.
  return { id };
}

describe("ReportEditorPage", () => {
  const getReportMock = vi.mocked(reportsApi.getReport);
  const saveLayoutMock = vi.mocked(reportsApi.saveLayout);
  const runQueryMock = vi.mocked(reportsApi.runQuery);
  const deleteReportMock = vi.mocked(reportsApi.deleteReport);
  const listVersionsMock = vi.mocked(reportsApi.listVersions);
  const restoreVersionMock = vi.mocked(reportsApi.restoreVersion);
  const updateReportMock = vi.mocked(reportsApi.updateReport);
  const duplicateReportMock = vi.mocked(reportsApi.duplicateReport);

  beforeEach(() => {
    getReportMock.mockReset();
    saveLayoutMock.mockReset();
    runQueryMock.mockReset();
    deleteReportMock.mockReset();
    listVersionsMock.mockReset();
    restoreVersionMock.mockReset();
    updateReportMock.mockReset();
    duplicateReportMock.mockReset();
    runQueryMock.mockResolvedValue({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 1 },
    });
    pushMock.mockReset();
    replaceMock.mockReset();
    // Default every test to desktop (no matchMedia → page treats as
    // desktop). Individual mobile tests opt into the small-screen stub.
    // @ts-expect-error -- clear any stub a prior test installed
    delete window.matchMedia;
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

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    // The editor renders inside AppShell so users keep the
    // sidebar/nav frame + shell-level surfaces.
    expect(screen.getByTestId("app-shell")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-empty")).toBeInTheDocument();
    // Empty-state guidance explains that canvas filters cascade into
    // every widget, so a 0-widget report shows nothing.
    expect(
      screen.getByText(/Add a widget to see your data/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/canvas date applies to every widget/i),
    ).toBeInTheDocument();

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

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    fireEvent.click(screen.getByTestId("report-editor-add-widget"));
    fireEvent.click(screen.getByTestId("widget-picker-option-kpi"));

    await waitFor(() =>
      expect(screen.getByTestId("kpi-widget")).toBeInTheDocument(),
    );
    // Picker dismisses after pick.
    expect(screen.queryByTestId("widget-picker")).toBeNull();
    // The newly added widget is selected → the editor popover mounts on
    // the render after the anchorEl effect resolves the shell node.
    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
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

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

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

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

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

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    // Report has widgets → opens in view mode. Enter edit mode so the
    // editor popover can mount on widget selection.
    await screen.findByTestId("kpi-widget");
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));

    // Click the rendered widget to select it; the popover mounts on the next
    // render (after ``useWidgetAnchor`` resolves the widget's shell node via
    // querySelector). The Canvas stub renders the widget directly, so this
    // click stands in for clicking the widget's shell on the real grid.
    fireEvent.click(screen.getByTestId("kpi-widget"));

    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
    );
    // The override pill now lives in the Filters tab, not an always-visible
    // rail — switch to it before asserting.
    fireEvent.click(screen.getByRole("tab", { name: /filters/i }));
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

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

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

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

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
    // Exited edit mode: the toggle now reads "Edit", Add widget + Cancel
    // are gone.
    expect(screen.getByTestId("report-editor-toggle-edit")).toHaveTextContent(
      "Edit",
    );
    expect(screen.queryByTestId("report-editor-add-widget")).toBeNull();
    expect(screen.queryByTestId("report-editor-cancel")).toBeNull();
    // No server save was issued by Cancel.
    expect(saveLayoutMock).not.toHaveBeenCalled();
  });

  // A report carrying at least one widget. Used by the view-mode /
  // toggle / history tests below.
  const REPORT_WITH_WIDGET = {
    id: 10,
    owner_user_id: 1,
    org_id: 1,
    visibility: "private" as const,
    name: "My report",
    description: null,
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
    canvas_filters_json: {},
    schema_version: 1,
    created_at: "2026-05-22T10:00:00",
    updated_at: "2026-05-22T10:00:00",
  };

  it("opens a report with widgets in view mode (Edit/History/Delete, no edit affordances)", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    // View-mode toolbar.
    expect(screen.getByTestId("report-editor-toggle-edit")).toHaveTextContent(
      "Edit",
    );
    expect(screen.getByTestId("report-editor-history")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-delete")).toBeInTheDocument();
    // No edit-only affordances.
    expect(screen.queryByTestId("report-editor-add-widget")).toBeNull();
    expect(screen.queryByTestId("report-editor-save")).toBeNull();
    expect(screen.queryByTestId("report-editor-cancel")).toBeNull();
  });

  it("Edit enters edit mode; Save is disabled until a change, which also reveals Cancel", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));

    // Edit-mode toolbar: Add widget + Save + History + Delete + Done.
    expect(screen.getByTestId("report-editor-add-widget")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-history")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-delete")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-toggle-edit")).toHaveTextContent(
      "Done",
    );
    // Save disabled initially (not dirty); Cancel hidden (not dirty).
    expect(screen.getByTestId("report-editor-save")).toBeDisabled();
    expect(screen.queryByTestId("report-editor-cancel")).toBeNull();

    // Make a change → Save enables, Cancel appears.
    fireEvent.click(screen.getByTestId("report-editor-add-widget"));
    fireEvent.click(screen.getByTestId("widget-picker-option-bar"));
    await waitFor(() =>
      expect(screen.getByTestId("bar-widget")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("report-editor-save")).not.toBeDisabled();
    expect(screen.getByTestId("report-editor-cancel")).toBeInTheDocument();
  });

  it("Done returns to view mode and Edit re-enters edit mode (toggle works)", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    // Enter edit.
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));
    expect(screen.getByTestId("report-editor-add-widget")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-toggle-edit")).toHaveTextContent(
      "Done",
    );
    // Done → view mode.
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));
    expect(screen.queryByTestId("report-editor-add-widget")).toBeNull();
    expect(screen.getByTestId("report-editor-toggle-edit")).toHaveTextContent(
      "Edit",
    );
    // Edit → back to edit mode.
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));
    expect(screen.getByTestId("report-editor-add-widget")).toBeInTheDocument();
  });

  it("opens a 0-widget (blank) report in edit mode", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 1,
      org_id: 1,
      visibility: "private",
      name: "Blank",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    });

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    // Edit mode: Add widget present, toggle reads "Done".
    expect(screen.getByTestId("report-editor-add-widget")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-toggle-edit")).toHaveTextContent(
      "Done",
    );
  });

  it("History lists versions and Restore re-hydrates from the restored report", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);
    listVersionsMock.mockResolvedValue([
      { id: 99, is_original: false, created_at: "2026-05-23T09:30:00" },
      { id: 1, is_original: true, created_at: "2026-05-22T10:00:00" },
    ]);
    // Restoring the original returns an empty layout.
    restoreVersionMock.mockResolvedValue({
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
      updated_at: "2026-05-22T10:00:05",
    });

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    fireEvent.click(screen.getByTestId("report-editor-history"));

    // Panel opens and lists both versions; original badged.
    await screen.findByTestId("report-history-panel");
    await waitFor(() =>
      expect(listVersionsMock).toHaveBeenCalledWith(10),
    );
    await screen.findByTestId("report-history-row-1");
    expect(screen.getByTestId("report-history-row-99")).toBeInTheDocument();
    expect(
      screen.getByTestId("report-history-original-badge"),
    ).toBeInTheDocument();

    // Restore the original → confirm. Scope to the confirm dialog
    // (the history panel is also a dialog).
    fireEvent.click(screen.getByTestId("report-history-restore-1"));
    const dialog = screen.getByRole("dialog", { name: "Restore version" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore" }));

    await waitFor(() =>
      expect(restoreVersionMock).toHaveBeenCalledWith(10, 1),
    );
    // Re-hydrated from the restored (empty) layout; panel closed; view mode.
    await waitFor(() =>
      expect(screen.queryByTestId("kpi-widget")).toBeNull(),
    );
    expect(screen.queryByTestId("report-history-panel")).toBeNull();
    expect(screen.getByTestId("report-editor-empty")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-toggle-edit")).toHaveTextContent(
      "Edit",
    );
  });

  it("toggles visibility to org as an editor and reflects the new state", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);
    updateReportMock.mockResolvedValue({
      ...REPORT_WITH_WIDGET,
      visibility: "org",
      updated_at: "2026-05-22T11:00:00",
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    const toggle = screen.getByTestId("report-editor-visibility-toggle");
    expect(toggle).not.toBeDisabled();
    // Starts private.
    expect(screen.getByTestId("report-editor-visibility")).toHaveTextContent(
      /private/i,
    );

    fireEvent.click(toggle);

    await waitFor(() =>
      expect(updateReportMock).toHaveBeenCalledWith(10, { visibility: "org" }),
    );
    // Local state reflects the shared visibility.
    await waitFor(() =>
      expect(
        screen.getByTestId("report-editor-visibility"),
      ).toHaveTextContent(/org/i),
    );
  });

  it("does not enable the visibility toggle for a non-editor (non-owner)", async () => {
    mockUser(true);
    // Report owned by someone else; viewer (user id 1) is not the owner.
    getReportMock.mockResolvedValue({
      ...REPORT_WITH_WIDGET,
      owner_user_id: 999,
      visibility: "org",
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    // No enabled toggle for a non-editor.
    expect(
      screen.queryByTestId("report-editor-visibility-toggle"),
    ).toBeNull();
  });

  it("hides edit affordances for a non-owner viewing a shared report", async () => {
    mockUser(true);
    // Report owned by someone else; viewer (user id 1) is not the owner.
    getReportMock.mockResolvedValue({
      ...REPORT_WITH_WIDGET,
      owner_user_id: 999,
      visibility: "org",
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    // The Edit/Done toggle is owner-only.
    expect(screen.queryByTestId("report-editor-toggle-edit")).toBeNull();
  });

  it("hides the empty-state 'Add widget' CTA for a non-owner", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue({
      id: 10,
      owner_user_id: 999, // viewer (id 1) is not the owner
      org_id: 1,
      visibility: "org",
      name: "Shared empty report",
      description: null,
      layout_json: { version: 1, widgets: [] },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:00",
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor-empty");
    // Owner-only CTA must not render for a view-only user; the backend
    // 403s their PATCH, so the picker would only build unsavable changes.
    expect(
      screen.queryByTestId("report-editor-empty-add-widget"),
    ).toBeNull();
    // The "Start from a template" link still shows (it only navigates).
    expect(
      screen.getByTestId("report-editor-empty-templates"),
    ).toBeInTheDocument();
  });

  it("duplicates the report and navigates to the new copy", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);
    duplicateReportMock.mockResolvedValue({
      ...REPORT_WITH_WIDGET,
      id: 77,
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    fireEvent.click(screen.getByTestId("report-editor-duplicate"));

    await waitFor(() =>
      expect(duplicateReportMock).toHaveBeenCalledWith(10),
    );
    await waitFor(() =>
      expect(pushMock).toHaveBeenCalledWith("/reports/77"),
    );
  });

  it("shows a 'Report saved' toast after a successful save", async () => {
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

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    fireEvent.click(screen.getByTestId("report-editor-add-widget"));
    fireEvent.click(screen.getByTestId("widget-picker-option-bar"));
    await waitFor(() =>
      expect(screen.getByTestId("bar-widget")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("report-editor-save"));

    const toast = await screen.findByTestId("report-editor-saved-toast");
    expect(toast).toHaveTextContent("Report saved");
    // Accessible polite live region.
    expect(toast).toHaveAttribute("role", "status");
  });

  it("after a successful save lands on the read-only view (stays on the report, not the list) and Edit re-enters the builder", async () => {
    mockUser(true);
    // Blank report → opens in edit mode so the user can build.
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
    // The save persists a layout that carries the bar widget the user
    // added, so the read-only view has content to render.
    saveLayoutMock.mockResolvedValue({
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
            id: "w_bar",
            type: "bar",
            title: "New bar chart",
            grid: { x: 0, y: 0, w: 6, h: 4 },
            config: {
              dataset: "transactions",
              measure: { agg: "sum", field: "amount" },
              dimensions: ["category"],
              sort: { by: "value", dir: "desc" },
              limit: 10,
              format: "currency",
            },
          },
        ],
      },
      canvas_filters_json: {},
      schema_version: 1,
      created_at: "2026-05-22T10:00:00",
      updated_at: "2026-05-22T10:00:01",
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    // Build a change so Save is enabled.
    fireEvent.click(screen.getByTestId("report-editor-add-widget"));
    fireEvent.click(screen.getByTestId("widget-picker-option-bar"));
    await waitFor(() =>
      expect(screen.getByTestId("bar-widget")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("report-editor-save"));
    await waitFor(() => expect(saveLayoutMock).toHaveBeenCalledTimes(1));

    // Stayed on the report (no navigation to the list) and dropped into
    // the read-only view: the Edit/Done toggle reads "Edit" and the
    // edit-only affordances are gone.
    await waitFor(() =>
      expect(
        screen.getByTestId("report-editor-toggle-edit"),
      ).toHaveTextContent("Edit"),
    );
    expect(pushMock).not.toHaveBeenCalled();
    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("report-editor-add-widget")).toBeNull();
    expect(screen.queryByTestId("report-editor-save")).toBeNull();
    expect(screen.queryByTestId("report-editor-cancel")).toBeNull();
    // The saved widget still renders in the read-only view.
    expect(screen.getByTestId("bar-widget")).toBeInTheDocument();
    // No editor popover in view mode.
    expect(screen.queryByTestId("widget-editor-popover")).toBeNull();

    // Edit re-enters the builder: edit-only affordances return.
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));
    expect(screen.getByTestId("report-editor-add-widget")).toBeInTheDocument();
    expect(screen.getByTestId("report-editor-toggle-edit")).toHaveTextContent(
      "Done",
    );
    // Re-entered cleanly: not dirty, so Save is disabled and Cancel hidden.
    expect(screen.getByTestId("report-editor-save")).toBeDisabled();
    expect(screen.queryByTestId("report-editor-cancel")).toBeNull();
  });

  it("on small screens renders a read-only widget stack with no edit affordances", async () => {
    mockMatchMedia(true);
    mockUser(true);
    getReportMock.mockResolvedValue({
      ...REPORT_WITH_WIDGET,
      // Two widgets out of grid order to exercise the (y, x) sort.
      layout_json: {
        version: 1 as const,
        widgets: [
          {
            id: "w_lower",
            type: "kpi" as const,
            title: "Lower",
            grid: { x: 0, y: 4, w: 3, h: 2 },
            config: {
              dataset: "transactions" as const,
              measure: { agg: "sum" as const, field: "amount" as const },
              format: "currency" as const,
            },
          },
          {
            id: "w_upper",
            type: "kpi" as const,
            title: "Upper",
            grid: { x: 0, y: 0, w: 3, h: 2 },
            config: {
              dataset: "transactions" as const,
              measure: { agg: "sum" as const, field: "amount" as const },
              format: "currency" as const,
            },
          },
        ],
      },
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("report-editor");
    // Read-only stack renders (NOT the grid Canvas).
    const stack = await screen.findByTestId("reports-canvas-stack");
    expect(stack).toBeInTheDocument();
    expect(screen.queryByTestId("reports-canvas")).toBeNull();
    // Both widgets render their data.
    expect(stack.querySelectorAll("[data-widget-id]").length).toBe(2);
    expect(screen.getAllByTestId("kpi-widget").length).toBe(2);
    // No edit affordances at all: no Edit toggle, Add, Save, Cancel.
    expect(screen.queryByTestId("report-editor-toggle-edit")).toBeNull();
    expect(screen.queryByTestId("report-editor-add-widget")).toBeNull();
    expect(screen.queryByTestId("report-editor-save")).toBeNull();
    expect(screen.queryByTestId("report-editor-cancel")).toBeNull();
  });

  it("orders widgets for the mobile stack by grid (y, then x)", () => {
    const widgets: Widget[] = [
      {
        id: "c",
        type: "kpi",
        title: "c",
        grid: { x: 6, y: 2, w: 3, h: 2 },
        config: { dataset: "transactions", measure: { agg: "sum", field: "amount" } },
      },
      {
        id: "a",
        type: "kpi",
        title: "a",
        grid: { x: 6, y: 0, w: 3, h: 2 },
        config: { dataset: "transactions", measure: { agg: "sum", field: "amount" } },
      },
      {
        id: "b",
        type: "kpi",
        title: "b",
        grid: { x: 0, y: 0, w: 3, h: 2 },
        config: { dataset: "transactions", measure: { agg: "sum", field: "amount" } },
      },
    ];
    expect(orderWidgetsForStack(widgets).map((w) => w.id)).toEqual([
      "b",
      "a",
      "c",
    ]);
    // Pure function: does not mutate the input order.
    expect(widgets.map((w) => w.id)).toEqual(["c", "a", "b"]);
  });

  it("redirects to /dashboard when features.reports is false (per-org off)", async () => {
    mockUser(false);
    getReportMock.mockResolvedValue({
      id: 10,
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith("/dashboard"),
    );
    expect(getReportMock).not.toHaveBeenCalled();
  });

  it("fetches and renders the report when features.reports is true (per-org override on)", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);
    runQueryMock.mockResolvedValue({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await waitFor(() => expect(getReportMock).toHaveBeenCalled());
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });

  it("opening the widget editor popover does not reflow the canvas (saved editor)", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    // Report has a widget → opens in view mode; enter edit mode.
    await screen.findByTestId("kpi-widget");
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));

    // Before selection: the canvas column is the SOLE element-child of the
    // flex row (no rail sibling stealing width).
    const canvasCol = screen.getByTestId("report-canvas-column");
    const flexRow = canvasCol.parentElement!;
    expect(flexRow.children).toHaveLength(1);
    expect(flexRow.children[0]).toBe(canvasCol);

    // Select the widget → the popover mounts on the next render.
    fireEvent.click(screen.getByTestId("kpi-widget"));
    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
    );

    // The popover is portaled to document.body, NOT inside the canvas column.
    expect(
      canvasCol.contains(screen.getByTestId("widget-editor-popover")),
    ).toBe(false);

    // After selection: the canvas column is STILL the only element-child of
    // the flex row — the popover never became a flex sibling, so the canvas
    // geometry/width is unchanged (the reflow regression guard).
    expect(flexRow.children).toHaveLength(1);
    expect(flexRow.children[0]).toBe(canvasCol);
  });

  it("clicking a widget filter chip selects the widget and opens the popover on Filters", async () => {
    mockUser(true);
    // Widget carries a txn_type filter → renders a filter chip.
    getReportMock.mockResolvedValue({
      ...REPORT_WITH_WIDGET,
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
              filters: { txn_type: "expense" as const },
            },
          },
        ],
      },
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    // View mode opens for a report with widgets; enter edit mode so the
    // chip's real handler is wired (view mode passes a no-op).
    await screen.findByTestId("kpi-widget");
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));

    // Chip renders in the shell header. Click it → widget selected +
    // popover deep-links to the Filters tab.
    fireEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));
    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("tab", { name: /filters/i }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("a SECOND chip click on the already-selected widget returns the popover to Filters", async () => {
    // Page-level analog of the popover unit test's consume-and-clear: a
    // second chip click on the SAME widget (after the user navigated away
    // to Data) must re-deep-link to the Filters tab. This exercises the
    // requestedTab handshake end-to-end through the page wiring.
    mockUser(true);
    getReportMock.mockResolvedValue({
      ...REPORT_WITH_WIDGET,
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
              filters: { txn_type: "expense" as const },
            },
          },
        ],
      },
    } as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    await screen.findByTestId("kpi-widget");
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));

    // First chip click → popover opens deep-linked to Filters.
    fireEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));
    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("tab", { name: /filters/i }),
    ).toHaveAttribute("aria-selected", "true");

    // User manually switches to the Data tab.
    fireEvent.click(screen.getByRole("tab", { name: /data/i }));
    expect(
      screen.getByRole("tab", { name: /data/i }),
    ).toHaveAttribute("aria-selected", "true");

    // Second chip click on the SAME (already-selected) widget → back to Filters.
    fireEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));
    await waitFor(() =>
      expect(
        screen.getByRole("tab", { name: /filters/i }),
      ).toHaveAttribute("aria-selected", "true"),
    );
  });

  it("edits a widget through the popover and the change round-trips into the saved layout", async () => {
    mockUser(true);
    getReportMock.mockResolvedValue(REPORT_WITH_WIDGET as never);
    saveLayoutMock.mockResolvedValue(REPORT_WITH_WIDGET as never);

    renderWithSWR(<ReportEditorPage params={makeParams()} />);

    // Report has a widget → opens in view mode; enter edit mode and select.
    await screen.findByTestId("kpi-widget");
    fireEvent.click(screen.getByTestId("report-editor-toggle-edit"));
    fireEvent.click(screen.getByTestId("kpi-widget"));

    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
    );

    // The title control lives in the Style tab — switch to it, then rename
    // the widget through the popover's title input.
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    const titleInput = screen.getByLabelText("Widget title");
    expect(titleInput).toHaveValue("Total");
    fireEvent.change(titleInput, { target: { value: "Net total" } });
    // The popover reflects the new value immediately (controlled input fed
    // by the page's updateWidget state).
    expect(screen.getByLabelText("Widget title")).toHaveValue("Net total");

    // Persist and assert the edit reached the saved layout — i.e. the
    // popover's onUpdate path actually mutated widget state on the page.
    fireEvent.click(screen.getByTestId("report-editor-save"));
    await waitFor(() => expect(saveLayoutMock).toHaveBeenCalledTimes(1));
    const [, savedLayout] = saveLayoutMock.mock.calls[0];
    expect(savedLayout.widgets).toHaveLength(1);
    expect(savedLayout.widgets[0].id).toBe("w_kpi");
    expect(savedLayout.widgets[0].title).toBe("Net total");
  });
});
