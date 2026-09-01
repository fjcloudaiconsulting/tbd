/**
 * TBD-430 — the page-scoped notice (tenant 3).
 *
 * `/reports/sources` is ONE constant SWR key, so its failure is one fact
 * about the whole canvas and never a per-widget notice. This file fences
 * the three states, and it is UNIMPLEMENTABLE until `useReportSources`
 * stops swallowing SWR's `error` — which is the point: today "the catalog
 * is down" is byte-identical to "the catalog is empty".
 */
// ⚠ `renderWithSWR`, not a bare `render`. `/api/v1/reports/sources` is a
// CONSTANT SWR key, so the module-scoped default cache would carry one
// test's resolved catalog into the next — the "happy path" case would then
// silently assert against the previous test's empty array and pass for the
// wrong reason.
import { renderWithSWR, screen, waitFor } from "../utils/render-with-swr";

import ReportDraftPage from "@/app/reports/new/page";
import ReportEditorPage from "@/app/reports/[id]/page";
import * as reportsApi from "@/lib/reports/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { DEFAULT_FEATURES } from "@/lib/features";
import { ALL_ENTRIES } from "../utils/mock-report-sources";

type SourcesMode = "reject" | "empty" | "pending" | "ok";
let sourcesMode: SourcesMode = "ok";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => {
    if (path.startsWith("/api/v1/reports/sources")) {
      if (sourcesMode === "reject") return Promise.reject(new Error("500"));
      if (sourcesMode === "empty") return Promise.resolve([]);
      if (sourcesMode === "pending") return new Promise(() => {});
      return Promise.resolve(ENTRIES);
    }
    return Promise.reject(new Error(`unmocked apiFetch: ${path}`));
  },
}));

vi.mock("@/lib/reports/api", () => ({
  createReport: vi.fn(),
  listTemplates: vi.fn(),
  getReport: vi.fn(),
  saveLayout: vi.fn(),
  runQuery: vi.fn(),
  deleteReport: vi.fn(),
  listVersions: vi.fn(),
  restoreVersion: vi.fn(),
  updateReport: vi.fn(),
  duplicateReport: vi.fn(),
}));

// jsdom cannot measure the react-grid-layout container; the stub keeps the
// render-tree shape without the canvas.
vi.mock("@/components/reports/Canvas", () => ({
  default: () => <div data-testid="reports-canvas" />,
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/reports/new",
  useSearchParams: () => new URLSearchParams(),
}));

const ENTRIES = ALL_ENTRIES;

const EMPTY_REPORT = {
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
};

const BANNER =
  "The data-source catalog is unavailable. Some widgets may be missing " +
  "units or failing to load.";

describe("report canvas — data-source catalog banner", () => {
  beforeEach(() => {
    sourcesMode = "ok";
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    vi.mocked(reportsApi.getReport).mockResolvedValue(EMPTY_REPORT as never);
    vi.mocked(reportsApi.listTemplates).mockResolvedValue([]);
    vi.mocked(useAuth).mockReturnValue({
      user: { id: 1 } as never,
      loading: false,
      needsSetup: false,
      features: { ...DEFAULT_FEATURES, reports: true, plans: false },
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  it("shows ONE banner naming the real blast radius when /sources rejects", async () => {
    sourcesMode = "reject";
    renderWithSWR(<ReportDraftPage />);
    const banner = await screen.findByTestId("report-sources-unavailable");
    expect(banner).toHaveTextContent(BANNER);
    expect(screen.getAllByTestId("report-sources-unavailable")).toHaveLength(1);
  });

  // KILLS: `sources.length === 0` used as the failure predicate. An empty
  // catalog is a legitimate answer, not an outage.
  it("shows NO banner when /sources resolves to an empty catalog", async () => {
    sourcesMode = "empty";
    renderWithSWR(<ReportDraftPage />);
    await screen.findByTestId("canvas-filters-bar");
    await waitFor(() =>
      expect(
        screen.queryByTestId("report-sources-unavailable"),
      ).toBeNull(),
    );
  });

  // KILLS: `!isLoading && sources.length === 0` — still in flight is not
  // an outage either.
  it("shows NO banner while /sources is still in flight", async () => {
    sourcesMode = "pending";
    renderWithSWR(<ReportDraftPage />);
    await screen.findByTestId("canvas-filters-bar");
    expect(screen.queryByTestId("report-sources-unavailable")).toBeNull();
  });

  it("shows NO banner on the happy path", async () => {
    renderWithSWR(<ReportDraftPage />);
    await screen.findByTestId("canvas-filters-bar");
    await waitFor(() =>
      expect(screen.queryByTestId("report-sources-unavailable")).toBeNull(),
    );
  });

  // ⚠ The SAVED-report canvas is a SECOND consumer. Covering the draft
  // page only would record coverage of the banner, not of the PATH: the
  // two pages have independent JSX and a fix landed in one of them is
  // invisible in the other.
  it("mirrors the banner on the saved-report canvas", async () => {
    sourcesMode = "reject";
    renderWithSWR(<ReportEditorPage params={{ id: "10" }} />);
    const banner = await screen.findByTestId("report-sources-unavailable");
    expect(banner).toHaveTextContent(BANNER);
    expect(screen.getAllByTestId("report-sources-unavailable")).toHaveLength(1);
  });

  it("shows NO banner on the saved-report canvas when the catalog loads", async () => {
    renderWithSWR(<ReportEditorPage params={{ id: "10" }} />);
    await screen.findByTestId("report-editor");
    await waitFor(() =>
      expect(screen.queryByTestId("report-sources-unavailable")).toBeNull(),
    );
  });
});
