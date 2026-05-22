import React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { SWRConfig } from "swr";

// vitest.setup.ts mocks ``@/components/notifications/NotificationBell``
// globally so AppShell-mounting page tests don't trip on the
// /api/v1/notifications poll. THIS file needs the real component.
vi.unmock("@/components/notifications/NotificationBell");

import NotificationBell from "@/components/notifications/NotificationBell";
import { apiFetch } from "@/lib/api";
import type { Notification } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/dashboard",
}));

const mockedApiFetch = vi.mocked(apiFetch);

function mkNotification(
  id: number,
  overrides: Partial<Notification> = {},
): Notification {
  return {
    id,
    category: "security",
    event_type: "user.password.changed",
    title: `Notification ${id}`,
    body: "Body text",
    link_url: "/settings/security",
    seen_at: null,
    read_at: null,
    audit_event_id: 100 + id,
    created_at: "2026-05-22T17:00:00",
    ...overrides,
  };
}

function renderBell() {
  return render(
    // Disable de-duping cache so each test gets a clean slate of
    // SWR state — without this, the in-memory cache from a prior
    // test bleeds into the next render and the mocked apiFetch is
    // never re-invoked.
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <NotificationBell />
    </SWRConfig>,
  );
}

/**
 * URL-aware default mock for apiFetch.
 *
 * The bell now fires two separate GETs on mount — the unseen-count
 * endpoint backs the badge, the list endpoint backs the popover
 * preview — plus a POST mark-seen on open. Routing by URL makes the
 * tests resilient to call ordering across SWR's two concurrent
 * fetches.
 */
function installRouteMock({
  count = 0,
  items = [] as Notification[],
}: { count?: number; items?: Notification[] }): void {
  mockedApiFetch.mockImplementation((path: string, opts?: RequestInit) => {
    if (
      typeof path === "string" &&
      path.endsWith("/api/v1/notifications/unseen-count")
    ) {
      return Promise.resolve({ count });
    }
    if (
      typeof path === "string" &&
      path.endsWith("/api/v1/notifications/mark-seen") &&
      opts?.method === "POST"
    ) {
      return Promise.resolve(undefined);
    }
    if (
      typeof path === "string" &&
      path.startsWith("/api/v1/notifications") &&
      (!opts || opts.method === undefined || opts.method === "GET")
    ) {
      return Promise.resolve({ items, next_cursor: null });
    }
    return Promise.resolve(undefined);
  });
}

describe("NotificationBell", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("renders without a badge when the unseen count is zero", async () => {
    installRouteMock({ count: 0, items: [] });

    await act(async () => {
      renderBell();
    });

    // Bell button is present.
    expect(
      screen.getByRole("button", { name: /notifications/i }),
    ).toBeInTheDocument();
    // No badge.
    expect(screen.queryByTestId("notification-badge")).toBeNull();
  });

  it("renders a numeric badge from the unseen-count endpoint", async () => {
    installRouteMock({
      count: 3,
      items: [mkNotification(1), mkNotification(2), mkNotification(3)],
    });

    await act(async () => {
      renderBell();
    });

    await waitFor(() => {
      expect(screen.getByTestId("notification-badge")).toHaveTextContent("3");
    });
    // aria-label reflects the unseen count.
    expect(
      screen.getByRole("button", { name: /notifications, 3 unseen/i }),
    ).toBeInTheDocument();
  });

  it("caps the badge label at 99+ above the threshold", async () => {
    // The count endpoint returns 120 — well above the list limit of
    // 10. The bell must still display 99+ from the count, not be
    // capped by the list size.
    installRouteMock({ count: 120, items: [] });

    await act(async () => {
      renderBell();
    });

    await waitFor(() => {
      expect(screen.getByTestId("notification-badge")).toHaveTextContent(
        "99+",
      );
    });
  });

  it("does not show a badge when the count is zero even with seen items in the list", async () => {
    installRouteMock({
      count: 0,
      items: [
        mkNotification(1, { seen_at: "2026-05-22T17:01:00" }),
        mkNotification(2, { seen_at: "2026-05-22T17:01:00" }),
      ],
    });

    await act(async () => {
      renderBell();
    });

    await waitFor(() => {
      expect(screen.queryByTestId("notification-badge")).toBeNull();
    });
  });

  it("opens the popover on click and fires mark-seen", async () => {
    installRouteMock({
      count: 1,
      items: [mkNotification(1)],
    });

    await act(async () => {
      renderBell();
    });

    await waitFor(() => {
      expect(screen.getByTestId("notification-badge")).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId("notification-bell"));
    });

    // Popover dialog appears.
    expect(
      screen.getByRole("dialog", { name: /notifications/i }),
    ).toBeInTheDocument();
    // mark-seen POST was fired.
    const calls = mockedApiFetch.mock.calls;
    const markSeenCall = calls.find(
      ([url, opts]) =>
        typeof url === "string" &&
        url.endsWith("/api/v1/notifications/mark-seen") &&
        (opts as RequestInit | undefined)?.method === "POST",
    );
    expect(markSeenCall).toBeDefined();
  });
});
