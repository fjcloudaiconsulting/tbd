import React from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";

import NotificationPopover from "@/components/notifications/NotificationPopover";
import { apiFetch } from "@/lib/api";
import type { Notification } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return { ...actual, apiFetch: vi.fn() };
});

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
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
    body: `Body for ${id}`,
    link_url: "/settings/security",
    seen_at: null,
    read_at: null,
    audit_event_id: 100 + id,
    created_at: "2026-05-22T17:00:00",
    ...overrides,
  };
}

describe("NotificationPopover", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    pushMock.mockReset();
  });

  it("renders an empty-state message when there are no items", () => {
    render(
      <NotificationPopover
        items={[]}
        onAfterReadChange={() => {}}
        onClose={() => {}}
      />,
    );
    expect(
      screen.getByText(/you'?re all caught up/i),
    ).toBeInTheDocument();
  });

  it("footer 'View all' links to the notifications settings page and closes on click", () => {
    const onClose = vi.fn();
    render(
      <NotificationPopover
        items={[]}
        onAfterReadChange={() => {}}
        onClose={onClose}
      />,
    );
    const link = screen.getByRole("link", { name: /view all/i });
    expect(link).toHaveAttribute("href", "/settings/notifications");
    // Cancel the anchor's default action so jsdom doesn't log an
    // unimplemented-navigation warning; the onClick (onClose) still fires.
    link.addEventListener("click", (e) => e.preventDefault());
    fireEvent.click(link);
    expect(onClose).toHaveBeenCalled();
  });

  it("caps the list to 10 rows even when more are passed", () => {
    const many = Array.from({ length: 15 }, (_, i) =>
      mkNotification(i + 1, { title: `Row ${i + 1}` }),
    );
    render(
      <NotificationPopover
        items={many}
        onAfterReadChange={() => {}}
        onClose={() => {}}
      />,
    );
    const list = screen.getByTestId("notification-list");
    const rows = within(list).getAllByRole("button");
    expect(rows).toHaveLength(10);
  });

  it("renders severity dots that vary by category", () => {
    const items = [
      mkNotification(1, { category: "security" }),
      mkNotification(2, { category: "account" }),
      mkNotification(3, { category: "org_admin" }),
      mkNotification(4, { category: "org_activity" }),
    ];
    render(
      <NotificationPopover
        items={items}
        onAfterReadChange={() => {}}
        onClose={() => {}}
      />,
    );
    // Security dot uses the danger token; the other three use
    // neutral / muted classes. Class-based assertions match the
    // SEVERITY_DOT mapping in the component.
    expect(
      screen.getByTestId("severity-dot-security").className,
    ).toContain("bg-danger");
    expect(
      screen.getByTestId("severity-dot-account").className,
    ).toContain("bg-text-muted");
    expect(
      screen.getByTestId("severity-dot-org_admin").className,
    ).toContain("bg-text-muted");
    expect(
      screen.getByTestId("severity-dot-org_activity").className,
    ).toContain("bg-text-muted/40");
  });

  it("marks a row read and navigates on click", async () => {
    mockedApiFetch.mockResolvedValueOnce(mkNotification(1, { read_at: "2026-05-22T17:05:00" }));

    const onAfterReadChange = vi.fn();
    const onClose = vi.fn();

    render(
      <NotificationPopover
        items={[mkNotification(1, { link_url: "/settings/security" })]}
        onAfterReadChange={onAfterReadChange}
        onClose={onClose}
      />,
    );

    const row = screen.getByRole("button", { name: /notification 1/i });
    await act(async () => {
      fireEvent.click(row);
    });

    // PATCH /api/v1/notifications/1 with {read: true}
    expect(mockedApiFetch).toHaveBeenCalledWith(
      "/api/v1/notifications/1",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ read: true }),
      }),
    );
    // Navigated to link_url.
    expect(pushMock).toHaveBeenCalledWith("/settings/security");
    // Closed.
    expect(onClose).toHaveBeenCalled();
  });

  it("does not crash when a row has no link_url", async () => {
    mockedApiFetch.mockResolvedValueOnce(mkNotification(2));

    const onClose = vi.fn();
    render(
      <NotificationPopover
        items={[mkNotification(2, { link_url: null })]}
        onAfterReadChange={() => {}}
        onClose={onClose}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /notification 2/i }));
    });

    // No router.push because link_url was null.
    expect(pushMock).not.toHaveBeenCalled();
    // Popover still closes.
    expect(onClose).toHaveBeenCalled();
  });
});
