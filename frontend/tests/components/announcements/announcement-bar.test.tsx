import React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

// vitest.setup.ts mocks ``@/components/announcements/AnnouncementBar``
// globally to a no-op so AppShell-mounting page tests don't trip on
// its hidden ``/api/v1/announcements`` fetch. THIS test file needs
// the real component, so we explicitly unmock before the import.
vi.unmock("@/components/announcements/AnnouncementBar");

import AnnouncementBar, {
  Announcement,
} from "@/components/announcements/AnnouncementBar";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/dashboard",
}));

const mockedApiFetch = vi.mocked(apiFetch);

function mkRow(
  id: number,
  severity: Announcement["severity"],
  title: string,
  body = "Body",
): Announcement {
  return {
    id,
    title,
    body,
    severity,
    is_active: true,
    start_at: null,
    end_at: null,
    created_at: "2026-05-22T00:00:00",
    updated_at: "2026-05-22T00:00:00",
  };
}

describe("AnnouncementBar", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("renders nothing when the API returns an empty array", async () => {
    mockedApiFetch.mockResolvedValueOnce([]);
    const { container } = render(<AnnouncementBar />);
    // Initial render is empty before the effect resolves; flush.
    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalled());
    expect(container.querySelector("[data-testid='announcement-bar']")).toBeNull();
  });

  it("renders nothing when the fetch fails", async () => {
    mockedApiFetch.mockRejectedValueOnce(new Error("network"));
    const { container } = render(<AnnouncementBar />);
    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalled());
    expect(container.querySelector("[data-testid='announcement-bar']")).toBeNull();
  });

  // ── regression: the bar is mounted globally in AppShell. A
  // foreign test that mocks apiFetch with a non-array shape (e.g.
  // `undefined` because the path-matcher fell through, or an error
  // envelope from a future contract) MUST NOT crash render with
  // `items.map is not a function`. PR #340 first revision crashed
  // 117 unrelated frontend tests this way. ───────────────────────
  it.each([
    ["null", null],
    ["undefined", undefined],
    ["error envelope", { error: "boom" }],
    ["empty array", []],
    ["string", "nope"],
    ["number", 42],
  ])("renders nothing without throwing when apiFetch resolves with %s", async (
    _label,
    payload,
  ) => {
    mockedApiFetch.mockResolvedValueOnce(payload as unknown as never);
    const { container } = render(<AnnouncementBar />);
    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalled());
    expect(
      container.querySelector("[data-testid='announcement-bar']"),
    ).toBeNull();
    // No row was rendered.
    expect(
      container.querySelector("[data-testid='announcement-row']"),
    ).toBeNull();
  });

  it("renders multiple rows in the order returned by the API", async () => {
    mockedApiFetch.mockResolvedValueOnce([
      mkRow(1, "maintenance", "Maintenance"),
      mkRow(2, "promo", "Promo"),
      mkRow(3, "info", "Info"),
    ]);
    render(<AnnouncementBar />);
    await waitFor(() => {
      expect(screen.getAllByTestId("announcement-row").length).toBe(3);
    });
    const rows = screen.getAllByTestId("announcement-row");
    expect(rows[0].getAttribute("data-severity")).toBe("maintenance");
    expect(rows[1].getAttribute("data-severity")).toBe("promo");
    expect(rows[2].getAttribute("data-severity")).toBe("info");
  });

  it("does NOT render a dismiss button on maintenance rows", async () => {
    mockedApiFetch.mockResolvedValueOnce([
      mkRow(1, "maintenance", "Heads up"),
    ]);
    render(<AnnouncementBar />);
    await screen.findByText("Heads up");
    expect(screen.queryByTestId("announcement-dismiss")).toBeNull();
  });

  it("renders a dismiss button on info and promo rows", async () => {
    mockedApiFetch.mockResolvedValueOnce([
      mkRow(1, "info", "i"),
      mkRow(2, "promo", "p"),
    ]);
    render(<AnnouncementBar />);
    await waitFor(() => {
      expect(screen.getAllByTestId("announcement-dismiss").length).toBe(2);
    });
  });

  it("optimistically removes a dismissed row and POSTs to the API", async () => {
    mockedApiFetch
      .mockResolvedValueOnce([
        mkRow(1, "info", "Dismiss me"),
        mkRow(2, "promo", "Stay"),
      ])
      .mockResolvedValueOnce(undefined);
    render(<AnnouncementBar />);
    await screen.findByText("Dismiss me");

    const dismissBtn = screen.getAllByTestId("announcement-dismiss")[0];
    fireEvent.click(dismissBtn);

    await waitFor(() => {
      expect(screen.queryByText("Dismiss me")).toBeNull();
    });
    expect(screen.getByText("Stay")).toBeInTheDocument();

    const postCall = mockedApiFetch.mock.calls.find(
      (c) => c[0] === "/api/v1/announcements/1/dismiss",
    );
    expect(postCall).toBeTruthy();
    expect(postCall?.[1]?.method).toBe("POST");
  });

  it("restores the row when the dismiss POST fails", async () => {
    mockedApiFetch
      .mockResolvedValueOnce([mkRow(1, "info", "Dismiss me")])
      .mockRejectedValueOnce(new Error("offline"));
    render(<AnnouncementBar />);
    await screen.findByText("Dismiss me");

    const dismissBtn = screen.getByTestId("announcement-dismiss");
    fireEvent.click(dismissBtn);

    // First the row disappears optimistically; then comes back.
    await waitFor(() => {
      expect(screen.queryByText("Dismiss me")).toBeInTheDocument();
    });
  });

  it("auto-linkifies http(s) URLs in the body", async () => {
    mockedApiFetch.mockResolvedValueOnce([
      mkRow(
        1,
        "info",
        "Visit",
        "See https://example.com/path for details.",
      ),
    ]);
    const { container } = render(<AnnouncementBar />);
    await screen.findByText("Visit");
    const link = container.querySelector(
      "a[href='https://example.com/path']",
    );
    expect(link).not.toBeNull();
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link?.getAttribute("target")).toBe("_blank");
  });

  it("does NOT linkify non-http schemes like javascript: or data:", async () => {
    mockedApiFetch.mockResolvedValueOnce([
      mkRow(
        1,
        "info",
        "Sketchy",
        "Click javascript:alert(1) or data:text/html,<x>",
      ),
    ]);
    const { container } = render(<AnnouncementBar />);
    await screen.findByText("Sketchy");
    // No anchor element at all — the matcher only accepts http(s).
    expect(container.querySelector("a")).toBeNull();
  });
});
