/**
 * Draft editor (/reports/new) widget-editor popover + reflow-invariance.
 *
 * The draft page is ALWAYS in edit mode (no ``editModeActive`` gate), so the
 * popover gate is ``selectedWidget && anchorEl`` only. These tests pin that
 * selecting a widget mounts the popover (on the second render, after the
 * ``anchorEl`` effect resolves the shell node) and that the popover never
 * becomes a flex sibling of the canvas column — proving the canvas does not
 * reflow when a widget is selected.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ReportDraftPage from "@/app/reports/new/page";
import * as reportsApi from "@/lib/reports/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/reports/api", () => ({
  createReport: vi.fn(),
  listTemplates: vi.fn(),
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

// Stub the canvas (react-grid-layout) — jsdom can't measure container width
// so the responsive grid collapses and never calls ``renderWidget``. The stub
// keeps the render-tree shape (calls ``renderWidget`` per widget) so the
// widget shells — and their ``data-widget-shell`` anchors — actually render.
vi.mock("@/components/reports/Canvas", () => ({
  default: ({
    layout,
    renderWidget,
  }: {
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

// The widget components fire SWR queries against the live API; stub them.
vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: ({ widget }: { widget: { title: string } }) => (
    <div data-testid="bar-widget">{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/KPIWidget", () => ({
  default: ({ widget }: { widget: { title: string } }) => (
    <div data-testid="kpi-widget">{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/LineWidget", () => ({
  default: ({ widget }: { widget: { title: string } }) => (
    <div data-testid="line-widget">{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/AreaWidget", () => ({
  default: ({ widget }: { widget: { title: string } }) => (
    <div data-testid="area-widget">{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/PieWidget", () => ({
  default: ({ widget }: { widget: { title: string } }) => (
    <div data-testid="pie-widget">{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/SparklineWidget", () => ({
  default: ({ widget }: { widget: { title: string } }) => (
    <div data-testid="sparkline-widget">{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/StackedBarWidget", () => ({
  default: ({ widget }: { widget: { title: string } }) => (
    <div data-testid="stacked-bar-widget">{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/TableWidget", () => ({
  default: ({ widget }: { widget: { title: string } }) => (
    <div data-testid="table-widget">{widget.title}</div>
  ),
}));

// Canvas filters bar pulls accounts/categories from the API; stub it.
vi.mock("@/components/reports/CanvasFiltersBar", () => ({
  default: () => <div data-testid="canvas-filters-bar" />,
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
let searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  usePathname: () => "/reports/new",
  useSearchParams: () => searchParams,
}));

function mockUser(reportsOn = true) {
  vi.mocked(useAuth).mockReturnValue({
    user: { id: 1 } as never,
    loading: false,
    needsSetup: false,
    features: { reports: reportsOn, plans: false },
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

describe("ReportDraftPage popover (/reports/new)", () => {
  const listTemplatesMock = vi.mocked(reportsApi.listTemplates);

  beforeEach(() => {
    vi.mocked(reportsApi.createReport).mockReset();
    listTemplatesMock.mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
    searchParams = new URLSearchParams();
    listTemplatesMock.mockResolvedValue([]);
  });

  it("mounts the widget editor popover when a widget is selected (always edit mode)", async () => {
    mockUser(true);

    render(<ReportDraftPage />);

    // The blank draft seeds a starter widget; select it.
    const widget = await screen.findByText("Spend by category");
    fireEvent.click(widget);

    // Popover mounts on the render AFTER the anchorEl effect resolves.
    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
    );
  });

  it("clicking a widget filter chip opens the popover on the Filters tab", async () => {
    mockUser(true);

    render(<ReportDraftPage />);

    // The seeded starter widget carries a txn_type filter → a chip.
    await screen.findByText("Spend by category");
    fireEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));

    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("tab", { name: /filters/i }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("opening the widget editor popover does not reflow the canvas (draft editor)", async () => {
    mockUser(true);

    render(<ReportDraftPage />);

    await screen.findByText("Spend by category");

    // Before selection: the canvas column is the SOLE element-child of the
    // flex row (no rail sibling stealing width).
    const canvasCol = screen.getByTestId("report-canvas-column");
    const flexRow = canvasCol.parentElement!;
    expect(flexRow.children).toHaveLength(1);
    expect(flexRow.children[0]).toBe(canvasCol);

    // Select the widget → popover mounts on the next render.
    fireEvent.click(screen.getByText("Spend by category"));
    await waitFor(() =>
      expect(screen.getByTestId("widget-editor-popover")).toBeInTheDocument(),
    );

    // The popover is portaled to document.body, not inside the canvas column.
    expect(
      canvasCol.contains(screen.getByTestId("widget-editor-popover")),
    ).toBe(false);

    // After selection: the canvas column is STILL the only element-child of
    // the flex row — the popover never became a flex sibling (reflow guard).
    expect(flexRow.children).toHaveLength(1);
    expect(flexRow.children[0]).toBe(canvasCol);
  });
});
