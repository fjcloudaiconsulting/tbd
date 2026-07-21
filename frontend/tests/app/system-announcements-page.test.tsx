import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SystemAnnouncementsPage from "@/app/system/announcements/page";
import { apiFetch } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/system/announcements",
}));

// The superadmin guard + AppShell chrome now live in AnnouncementsLayout
// (tested separately in tests/components/announcements-layout.test.tsx).
// The page itself is pure content, so pass children straight through.
vi.mock("@/components/AnnouncementsLayout", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="announcements-layout">{children}</div>
  ),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const SAMPLE_ROW = {
  id: 7,
  title: "Maintenance window",
  body: "We are upgrading the database.",
  severity: "maintenance" as const,
  is_active: true,
  start_at: null,
  end_at: null,
  created_at: "2026-05-22T00:00:00",
  updated_at: "2026-05-22T00:00:00",
  created_by_user_id: 1,
};

// GET /api/v1/admin/announcements now returns a ListEnvelope and the table
// appends sort + pagination query params, so match on the path prefix.
const isAnnouncementsGet = (url: unknown, options?: RequestInit): boolean =>
  typeof url === "string" &&
  url.startsWith("/api/v1/admin/announcements") &&
  !url.includes("/api/v1/admin/announcements/") &&
  !options?.method;
const annEnvelope = (items: unknown[]) =>
  Promise.resolve({ items, total: items.length, limit: 25, offset: 0 });

describe("/system/announcements page", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("lists existing rows for a superadmin", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (isAnnouncementsGet(url)) return annEnvelope([SAMPLE_ROW]);
      return Promise.resolve(undefined);
    }) as never);
    render(<SystemAnnouncementsPage />);
    await screen.findByText("Maintenance window");
    expect(screen.getByText(/Maintenance$/i)).toBeInTheDocument();
  });

  it("POSTs the form payload when creating a new announcement", async () => {
    apiFetchMock.mockImplementation(((url: string, options?: RequestInit) => {
      if (isAnnouncementsGet(url, options)) {
        return annEnvelope([]);
      }
      if (url === "/api/v1/admin/announcements" && options?.method === "POST") {
        return Promise.resolve(SAMPLE_ROW);
      }
      return Promise.resolve(undefined);
    }) as never);
    render(<SystemAnnouncementsPage />);
    await screen.findByTestId("announcement-empty");

    fireEvent.click(screen.getByTestId("announcement-new"));
    fireEvent.change(screen.getByTestId("announcement-form-title"), {
      target: { value: "New" },
    });
    fireEvent.change(screen.getByTestId("announcement-form-body"), {
      target: { value: "Body" },
    });
    fireEvent.change(screen.getByTestId("announcement-form-severity"), {
      target: { value: "promo" },
    });
    fireEvent.click(screen.getByTestId("announcement-form-submit"));

    await waitFor(() => {
      const postCall = apiFetchMock.mock.calls.find(
        (c) => c[0] === "/api/v1/admin/announcements" && c[1]?.method === "POST",
      );
      expect(postCall).toBeTruthy();
      const payload = JSON.parse(postCall![1]!.body as string);
      expect(payload.title).toBe("New");
      expect(payload.body).toBe("Body");
      expect(payload.severity).toBe("promo");
      expect(payload.is_active).toBe(true);
      expect(payload.start_at).toBeNull();
      expect(payload.end_at).toBeNull();
    });
  });

  it("pre-fills the form when editing", async () => {
    apiFetchMock.mockResolvedValueOnce({
      items: [SAMPLE_ROW],
      total: 1,
      limit: 25,
      offset: 0,
    } as never);
    render(<SystemAnnouncementsPage />);
    const editBtn = await screen.findByTestId("announcement-edit");
    fireEvent.click(editBtn);
    const titleInput = screen.getByTestId(
      "announcement-form-title",
    ) as HTMLInputElement;
    const bodyInput = screen.getByTestId(
      "announcement-form-body",
    ) as HTMLTextAreaElement;
    expect(titleInput.value).toBe("Maintenance window");
    expect(bodyInput.value).toBe("We are upgrading the database.");
  });
});
