import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SystemBroadcastsPage from "@/app/system/announcements/broadcasts/page";
import { createBroadcast, listBroadcasts } from "@/lib/broadcasts";
import type { Broadcast } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/system/announcements/broadcasts",
}));

// The superadmin guard + AppShell + tab-nav chrome live in AnnouncementsLayout
// (tested separately). Pass activeTab through as a data attribute so this
// suite can assert the broadcasts tab is the one the page requests.
vi.mock("@/components/AnnouncementsLayout", () => ({
  default: ({
    children,
    activeTab,
  }: {
    children: React.ReactNode;
    activeTab: string;
  }) => (
    <div data-testid="announcements-layout" data-active-tab={activeTab}>
      {children}
    </div>
  ),
}));

vi.mock("@/lib/broadcasts", () => ({
  listBroadcasts: vi.fn(),
  createBroadcast: vi.fn(),
}));

const listBroadcastsMock = vi.mocked(listBroadcasts);
const createBroadcastMock = vi.mocked(createBroadcast);

const SAMPLE_BROADCAST: Broadcast = {
  id: 7,
  subject: "Friday maintenance",
  body_template: "Hi {first_name}, we are upgrading things.",
  segment: "active_verified",
  status: "sending",
  created_by_user_id: 1,
  total_recipients: 200,
  sent_count: 42,
  failed_count: 1,
  skipped_count: 0,
  dry_run_sent_at: "2026-07-19T00:00:00",
  confirmed_at: "2026-07-19T00:05:00",
  created_at: "2026-07-19T00:00:00",
  started_at: "2026-07-19T00:05:00",
  completed_at: null,
  recipient_count: 200,
  delivered_count: 0,
  bounced_count: 0,
  soft_bounced_count: 0,
  complained_count: 0,
};

const DRAFT_BROADCAST: Broadcast = {
  id: 9,
  subject: "New draft",
  body_template: "Hello {first_name}",
  segment: "active_verified",
  status: "draft",
  created_by_user_id: 1,
  total_recipients: null,
  sent_count: 0,
  failed_count: 0,
  skipped_count: 0,
  dry_run_sent_at: null,
  confirmed_at: null,
  created_at: "2026-07-20T00:00:00",
  started_at: null,
  completed_at: null,
  recipient_count: 314,
  delivered_count: 0,
  bounced_count: 0,
  soft_bounced_count: 0,
  complained_count: 0,
};

describe("/system/announcements/broadcasts page", () => {
  beforeEach(() => {
    listBroadcastsMock.mockReset();
    createBroadcastMock.mockReset();
  });

  it("renders inside AnnouncementsLayout with the broadcasts tab active", async () => {
    listBroadcastsMock.mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 });
    render(<SystemBroadcastsPage />);
    const layout = await screen.findByTestId("announcements-layout");
    expect(layout).toHaveAttribute(
      "data-active-tab",
      "/system/announcements/broadcasts",
    );
  });

  it("lists broadcasts with a status badge and a Queued count, never Delivered", async () => {
    listBroadcastsMock.mockResolvedValue({
      items: [SAMPLE_BROADCAST],
      total: 1,
      limit: 25,
      offset: 0,
    });
    render(<SystemBroadcastsPage />);
    await screen.findByText("Friday maintenance");

    expect(screen.getByTestId("broadcast-queued-7")).toHaveTextContent("Queued");
    expect(screen.getByTestId("broadcast-queued-7")).toHaveTextContent("42");
    expect(screen.getByText(/sending/i)).toBeInTheDocument();
    expect(screen.queryByText(/delivered/i)).not.toBeInTheDocument();
    expect(screen.getByTestId("broadcast-row-item")).toBeInTheDocument();
  });

  it("shows the empty state when no broadcasts exist", async () => {
    listBroadcastsMock.mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 });
    render(<SystemBroadcastsPage />);
    await screen.findByTestId("broadcast-empty");
    expect(screen.getByText("No broadcasts yet.")).toBeInTheDocument();
  });

  it("submits the compose form and shows the returned draft with its recipient count", async () => {
    listBroadcastsMock.mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 });
    createBroadcastMock.mockResolvedValue(DRAFT_BROADCAST);

    render(<SystemBroadcastsPage />);
    await screen.findByTestId("broadcast-empty");

    fireEvent.change(screen.getByTestId("broadcast-form-subject"), {
      target: { value: "New draft" },
    });
    fireEvent.change(screen.getByTestId("broadcast-form-body"), {
      target: { value: "Hello {first_name}" },
    });
    fireEvent.click(screen.getByTestId("broadcast-form-submit"));

    await waitFor(() => {
      expect(createBroadcastMock).toHaveBeenCalledWith(
        "New draft",
        "Hello {first_name}",
      );
    });

    await screen.findByText("New draft");
    expect(screen.getByText(/314/)).toBeInTheDocument();
  });
});
