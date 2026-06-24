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

// The widget components fire SWR queries against the live API; stub
// them out so the draft canvas renders without network. We assert on
// the widget shell / title, not the rendered data.
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
    features: { reports: reportsOn, plans: false, customDashboard: false },
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

const TEMPLATE = {
  key: "monthly_review",
  name: "Monthly review",
  description: "x",
  layout_json: {
    version: 1 as const,
    widgets: [
      {
        id: "t_kpi",
        type: "kpi" as const,
        title: "Net income",
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
};

describe("ReportDraftPage (/reports/new)", () => {
  const createMock = vi.mocked(reportsApi.createReport);
  const listTemplatesMock = vi.mocked(reportsApi.listTemplates);

  beforeEach(() => {
    createMock.mockReset();
    listTemplatesMock.mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
    searchParams = new URLSearchParams();
    listTemplatesMock.mockResolvedValue([]);
  });

  it("renders a blank draft with the starter bar widget, no DB write", async () => {
    mockUser(true);

    render(<ReportDraftPage />);

    await screen.findByText("Spend by category");
    expect(createMock).not.toHaveBeenCalled();
  });

  it("loads a template draft when ?template is present", async () => {
    mockUser(true);
    searchParams = new URLSearchParams("template=monthly_review");
    listTemplatesMock.mockResolvedValue([TEMPLATE]);

    render(<ReportDraftPage />);

    await screen.findByText("Net income");
    expect(createMock).not.toHaveBeenCalled();
  });

  it("falls back to the blank draft when ?template is unknown", async () => {
    mockUser(true);
    searchParams = new URLSearchParams("template=does_not_exist");
    listTemplatesMock.mockResolvedValue([TEMPLATE]);

    render(<ReportDraftPage />);

    await screen.findByText("Spend by category");
    expect(createMock).not.toHaveBeenCalled();
  });

  it("persists only on Save, then navigates to the created report id", async () => {
    mockUser(true);
    createMock.mockResolvedValue({ id: 99 } as never);

    render(<ReportDraftPage />);

    await screen.findByText("Spend by category");
    fireEvent.click(screen.getByTestId("report-draft-save"));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    const payload = createMock.mock.calls[0][0];
    expect(payload.visibility).toBe("private");
    const layout = payload.layout_json as { widgets: unknown[] };
    expect(layout.widgets).toHaveLength(1);
    expect(payload.canvas_filters_json).toBeTruthy();

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/reports/99"));
  });

  it("discards the draft on Cancel without any DB write", async () => {
    mockUser(true);

    render(<ReportDraftPage />);

    await screen.findByText("Spend by category");
    fireEvent.click(screen.getByTestId("report-draft-cancel"));

    expect(createMock).not.toHaveBeenCalled();
    expect(pushMock).toHaveBeenCalledWith("/reports");
  });

  it("redirects to /dashboard when features.reports is false (per-org off)", async () => {
    mockUser(false);

    render(<ReportDraftPage />);

    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith("/dashboard"),
    );
    expect(createMock).not.toHaveBeenCalled();
  });

  it("renders draft canvas when features.reports is true (per-org override on)", async () => {
    mockUser(true);

    render(<ReportDraftPage />);

    await screen.findByText("Spend by category");
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });
});
