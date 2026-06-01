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

function mockUser(featureReportsV2 = true) {
  vi.mocked(useAuth).mockReturnValue({
    user: { id: 1 } as never,
    loading: false,
    needsSetup: false,
    featureReportsV2,
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
  layout_json: { version: 1 as const, widgets: [] },
  canvas_filters_json: {},
};

describe("ReportsListPage — templates", () => {
  const listMock = vi.mocked(reportsApi.listReports);
  const listTemplatesMock = vi.mocked(reportsApi.listTemplates);
  const createFromTemplateMock = vi.mocked(reportsApi.createFromTemplate);

  beforeEach(() => {
    listMock.mockReset();
    listTemplatesMock.mockReset();
    createFromTemplateMock.mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
  });

  it("renders the templates section from the API", async () => {
    mockUser(true);
    listMock.mockResolvedValue([]);
    listTemplatesMock.mockResolvedValue([TEMPLATE]);

    render(<ReportsListPage />);

    await screen.findByText("Monthly review");
    expect(screen.getByText("x")).toBeInTheDocument();
  });

  it("instantiates a template and navigates to its editor on 'Use template'", async () => {
    mockUser(true);
    listMock.mockResolvedValue([]);
    listTemplatesMock.mockResolvedValue([TEMPLATE]);
    createFromTemplateMock.mockResolvedValue({ id: 42 } as never);

    render(<ReportsListPage />);

    await screen.findByText("Monthly review");
    fireEvent.click(screen.getByRole("button", { name: /use template/i }));

    await waitFor(() =>
      expect(createFromTemplateMock).toHaveBeenCalledTimes(1),
    );
    expect(createFromTemplateMock).toHaveBeenCalledWith(TEMPLATE);
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/reports/42"));
  });
});
