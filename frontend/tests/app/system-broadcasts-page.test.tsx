import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import SystemBroadcastsPage from "@/app/system/announcements/broadcasts/page";
import {
  createBroadcast,
  deleteBroadcast,
  dryRunBroadcast,
  getBroadcast,
  listBroadcasts,
  listRecipients,
  previewBroadcast,
  resumeBroadcast,
  sendBroadcast,
} from "@/lib/broadcasts";
import { ApiResponseError } from "@/lib/api";
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

// broadcastErrorCode / BROADCAST_ERROR_COPY are kept real (importActual) so
// the coded-error-mapping tests exercise the actual R7.1 logic; only the
// network-touching calls are mocked.
vi.mock("@/lib/broadcasts", async () => {
  const actual = await vi.importActual<typeof import("@/lib/broadcasts")>(
    "@/lib/broadcasts",
  );
  return {
    ...actual,
    listBroadcasts: vi.fn(),
    createBroadcast: vi.fn(),
    getBroadcast: vi.fn(),
    previewBroadcast: vi.fn(),
    dryRunBroadcast: vi.fn(),
    sendBroadcast: vi.fn(),
    resumeBroadcast: vi.fn(),
    listRecipients: vi.fn(),
    deleteBroadcast: vi.fn(),
  };
});

const listBroadcastsMock = vi.mocked(listBroadcasts);
const createBroadcastMock = vi.mocked(createBroadcast);
const getBroadcastMock = vi.mocked(getBroadcast);
const previewBroadcastMock = vi.mocked(previewBroadcast);
const dryRunBroadcastMock = vi.mocked(dryRunBroadcast);
const sendBroadcastMock = vi.mocked(sendBroadcast);
const resumeBroadcastMock = vi.mocked(resumeBroadcast);
const listRecipientsMock = vi.mocked(listRecipients);
const deleteBroadcastMock = vi.mocked(deleteBroadcast);

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

const DRAFT_DRY_RUN_DONE: Broadcast = {
  ...DRAFT_BROADCAST,
  id: 10,
  subject: "Dry-run-done draft",
  dry_run_sent_at: "2026-07-20T00:01:00",
};

const COMPLETED_BROADCAST: Broadcast = {
  ...SAMPLE_BROADCAST,
  id: 11,
  status: "completed",
  sent_count: 199,
  failed_count: 1,
  completed_at: "2026-07-19T01:00:00",
  delivered_count: 150,
  bounced_count: 2,
  soft_bounced_count: 1,
  complained_count: 0,
};

const FAILED_BROADCAST: Broadcast = {
  ...SAMPLE_BROADCAST,
  id: 12,
  status: "failed",
  completed_at: "2026-07-19T01:00:00",
};

async function renderWithItems(items: Broadcast[]) {
  listBroadcastsMock.mockResolvedValue({ items, total: items.length, limit: 25, offset: 0 });
  render(<SystemBroadcastsPage />);
  await screen.findAllByTestId("broadcast-row-item");
}

async function openDetail(id: number) {
  fireEvent.click(screen.getByTestId(`broadcast-view-${id}`));
  await screen.findByTestId("broadcast-detail");
}

describe("/system/announcements/broadcasts page", () => {
  beforeEach(() => {
    listBroadcastsMock.mockReset();
    createBroadcastMock.mockReset();
    getBroadcastMock.mockReset();
    previewBroadcastMock.mockReset();
    dryRunBroadcastMock.mockReset();
    sendBroadcastMock.mockReset();
    resumeBroadcastMock.mockReset();
    listRecipientsMock.mockReset();
    deleteBroadcastMock.mockReset();
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

  describe("send flow (Task 5)", () => {
    it("shows Preview / Send-test / Send only for a draft; a completed broadcast shows none of them", async () => {
      await renderWithItems([DRAFT_BROADCAST, COMPLETED_BROADCAST]);

      await openDetail(DRAFT_BROADCAST.id);
      expect(screen.getByTestId("broadcast-preview-button")).toBeInTheDocument();
      expect(screen.getByTestId("broadcast-dry-run-button")).toBeInTheDocument();
      expect(screen.getByTestId("broadcast-send-button")).toBeInTheDocument();

      fireEvent.click(screen.getByTestId(`broadcast-view-${COMPLETED_BROADCAST.id}`));
      await waitFor(() => {
        expect(screen.queryByTestId("broadcast-preview-button")).not.toBeInTheDocument();
      });
      expect(screen.queryByTestId("broadcast-dry-run-button")).not.toBeInTheDocument();
      expect(screen.queryByTestId("broadcast-send-button")).not.toBeInTheDocument();
      // completed still shows progress + delivery, never a re-send control
      expect(screen.getByTestId("broadcast-progress")).toBeInTheDocument();
      expect(screen.getByTestId("broadcast-delivery")).toBeInTheDocument();
    });

    it("renders the preview text in a <pre>, never an iframe", async () => {
      await renderWithItems([DRAFT_BROADCAST]);
      await openDetail(DRAFT_BROADCAST.id);

      previewBroadcastMock.mockResolvedValue({
        subject: "New draft",
        html: "<p>should never render</p>",
        text: "Plain text preview body",
      });
      fireEvent.click(screen.getByTestId("broadcast-preview-button"));

      const pre = await screen.findByTestId("broadcast-preview-text");
      expect(pre.tagName).toBe("PRE");
      expect(pre).toHaveTextContent("Plain text preview body");
      expect(document.querySelector("iframe")).toBeNull();
      expect(screen.getByText(/send test to me shows the real rendered email/i)).toBeInTheDocument();
    });

    it("dry-run shows the sent-to-inbox message", async () => {
      await renderWithItems([DRAFT_BROADCAST]);
      await openDetail(DRAFT_BROADCAST.id);

      dryRunBroadcastMock.mockResolvedValue({ ...DRAFT_BROADCAST, dry_run_sent_at: "2026-07-20T00:02:00" });
      fireEvent.click(screen.getByTestId("broadcast-dry-run-button"));

      await screen.findByText("Test sent to your inbox.");
    });

    it("keeps the confirm Send button disabled until the typed count matches AND a dry-run has happened, then enables it", async () => {
      await renderWithItems([DRAFT_DRY_RUN_DONE]);
      await openDetail(DRAFT_DRY_RUN_DONE.id);

      fireEvent.click(screen.getByTestId("broadcast-send-button"));
      const modal = await screen.findByTestId("broadcast-send-modal");
      const confirmBtn = within(modal).getByTestId("broadcast-send-confirm-submit");
      expect(confirmBtn).toBeDisabled();

      fireEvent.change(within(modal).getByTestId("broadcast-send-confirm-count-input"), {
        target: { value: "1" },
      });
      fireEvent.click(within(modal).getByTestId("broadcast-send-confirm-checkbox"));
      expect(confirmBtn).toBeDisabled(); // count 1 != 314

      fireEvent.change(within(modal).getByTestId("broadcast-send-confirm-count-input"), {
        target: { value: "314" },
      });
      expect(confirmBtn).not.toBeDisabled();
    });

    it("Send stays disabled (can't even open the modal) when no dry-run has happened yet", async () => {
      await renderWithItems([DRAFT_BROADCAST]);
      await openDetail(DRAFT_BROADCAST.id);
      expect(screen.getByTestId("broadcast-send-button")).toBeDisabled();
    });

    it("a confirm_count_mismatch rejection shows friendly copy and triggers a getBroadcast refetch", async () => {
      await renderWithItems([DRAFT_DRY_RUN_DONE]);
      await openDetail(DRAFT_DRY_RUN_DONE.id);

      getBroadcastMock.mockResolvedValue({ ...DRAFT_DRY_RUN_DONE, recipient_count: 500 });
      sendBroadcastMock.mockRejectedValue(
        new ApiResponseError(409, "conflict", undefined, { code: "confirm_count_mismatch" }),
      );

      fireEvent.click(screen.getByTestId("broadcast-send-button"));
      const modal = await screen.findByTestId("broadcast-send-modal");
      fireEvent.change(within(modal).getByTestId("broadcast-send-confirm-count-input"), {
        target: { value: "314" },
      });
      fireEvent.click(within(modal).getByTestId("broadcast-send-confirm-checkbox"));
      fireEvent.click(within(modal).getByTestId("broadcast-send-confirm-submit"));

      await screen.findByText(/recipient count changed since this draft was loaded/i);
      await waitFor(() => expect(getBroadcastMock).toHaveBeenCalledWith(DRAFT_DRY_RUN_DONE.id));
    });

    it("an invalid_template_token rejection shows its friendly copy", async () => {
      await renderWithItems([DRAFT_DRY_RUN_DONE]);
      await openDetail(DRAFT_DRY_RUN_DONE.id);

      sendBroadcastMock.mockRejectedValue(
        new ApiResponseError(422, "unprocessable", undefined, { code: "invalid_template_token" }),
      );

      fireEvent.click(screen.getByTestId("broadcast-send-button"));
      const modal = await screen.findByTestId("broadcast-send-modal");
      fireEvent.change(within(modal).getByTestId("broadcast-send-confirm-count-input"), {
        target: { value: "314" },
      });
      fireEvent.click(within(modal).getByTestId("broadcast-send-confirm-checkbox"));
      fireEvent.click(within(modal).getByTestId("broadcast-send-confirm-submit"));

      await screen.findByText(/isn't allowed. remove it and try again/i);
    });

    it("disables Send while the request is in-flight (double-submit guard)", async () => {
      await renderWithItems([DRAFT_DRY_RUN_DONE]);
      await openDetail(DRAFT_DRY_RUN_DONE.id);

      let resolveSend: (b: Broadcast) => void = () => {};
      sendBroadcastMock.mockImplementation(
        () => new Promise((resolve) => { resolveSend = resolve; }),
      );

      fireEvent.click(screen.getByTestId("broadcast-send-button"));
      const modal = await screen.findByTestId("broadcast-send-modal");
      fireEvent.change(within(modal).getByTestId("broadcast-send-confirm-count-input"), {
        target: { value: "314" },
      });
      fireEvent.click(within(modal).getByTestId("broadcast-send-confirm-checkbox"));
      fireEvent.click(within(modal).getByTestId("broadcast-send-confirm-submit"));

      await waitFor(() => {
        expect(within(modal).getByTestId("broadcast-send-confirm-submit")).toBeDisabled();
      });

      await act(async () => {
        resolveSend({ ...DRAFT_DRY_RUN_DONE, status: "sending" });
        await Promise.resolve();
      });
    });

    it("polls while sending, showing Queued/Failed/Skipped counts, and stops on completed", async () => {
      vi.useFakeTimers();
      try {
        listBroadcastsMock.mockResolvedValue({
          items: [SAMPLE_BROADCAST],
          total: 1,
          limit: 25,
          offset: 0,
        });
        render(<SystemBroadcastsPage />);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });

        fireEvent.click(screen.getByTestId(`broadcast-view-${SAMPLE_BROADCAST.id}`));

        const progress = screen.getByTestId("broadcast-progress");
        expect(progress).toHaveTextContent("Queued 42");
        expect(progress).toHaveTextContent("Failed 1");
        expect(progress).toHaveTextContent("Skipped 0");

        getBroadcastMock.mockResolvedValue({
          ...SAMPLE_BROADCAST,
          sent_count: 200,
          status: "completed",
          completed_at: "2026-07-19T02:00:00",
        });

        await act(async () => {
          await vi.advanceTimersByTimeAsync(5000);
        });

        expect(getBroadcastMock).toHaveBeenCalledWith(SAMPLE_BROADCAST.id);
        expect(screen.getByTestId("broadcast-progress")).toHaveTextContent("Queued 200");

        getBroadcastMock.mockClear();
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5000);
        });
        expect(getBroadcastMock).not.toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    });

    it("shows a Resume button while sending (stalled)", async () => {
      await renderWithItems([SAMPLE_BROADCAST]);
      await openDetail(SAMPLE_BROADCAST.id);
      expect(screen.getByTestId("broadcast-resume-button")).toBeInTheDocument();
    });

    it("shows a Resume button when failed", async () => {
      await renderWithItems([FAILED_BROADCAST]);
      await openDetail(FAILED_BROADCAST.id);
      expect(screen.getByTestId("broadcast-resume-button")).toBeInTheDocument();
    });

    it("does not show Resume for a draft or a completed broadcast", async () => {
      await renderWithItems([DRAFT_BROADCAST, COMPLETED_BROADCAST]);
      await openDetail(DRAFT_BROADCAST.id);
      expect(screen.queryByTestId("broadcast-resume-button")).not.toBeInTheDocument();

      fireEvent.click(screen.getByTestId(`broadcast-view-${COMPLETED_BROADCAST.id}`));
      await waitFor(() => {
        expect(screen.queryByTestId("broadcast-resume-button")).not.toBeInTheDocument();
      });
    });

    it("shows the delivery breakdown with the Mailgun-webhook note, never labeling sent_count as Delivered", async () => {
      await renderWithItems([COMPLETED_BROADCAST]);
      await openDetail(COMPLETED_BROADCAST.id);

      const delivery = screen.getByTestId("broadcast-delivery");
      expect(delivery).toHaveTextContent("Delivered 150");
      expect(delivery).toHaveTextContent("Bounced 2");
      expect(delivery).toHaveTextContent("1 soft");
      expect(delivery).toHaveTextContent("Complaints 0");
      expect(delivery).toHaveTextContent(/populates as mailgun reports back/i);
    });

    it("view recipients lists rows with delivery_status", async () => {
      await renderWithItems([COMPLETED_BROADCAST]);
      await openDetail(COMPLETED_BROADCAST.id);

      listRecipientsMock.mockResolvedValue({
        items: [
          {
            id: 1,
            email: "a@example.com",
            first_name: "A",
            status: "sent",
            delivery_status: "delivered",
            delivery_updated_at: "2026-07-19T01:00:00",
            sent_at: "2026-07-19T00:30:00",
          },
          {
            id: 2,
            email: "b@example.com",
            first_name: "B",
            status: "sent",
            delivery_status: null,
            delivery_updated_at: null,
            sent_at: "2026-07-19T00:31:00",
          },
        ],
        total: 2,
        limit: 25,
        offset: 0,
      });

      fireEvent.click(screen.getByTestId("broadcast-view-recipients-button"));

      await waitFor(() => {
        expect(listRecipientsMock).toHaveBeenCalledWith(COMPLETED_BROADCAST.id, 0, 25);
      });
      const rows = await screen.findAllByTestId("broadcast-recipient-row");
      expect(rows).toHaveLength(2);
      expect(rows[0]).toHaveTextContent("a@example.com");
      expect(rows[0]).toHaveTextContent("delivered");
      expect(rows[1]).toHaveTextContent("b@example.com");
    });
  });

  describe("delete draft", () => {
    it("offers Delete only on draft rows, never on sent/completed ones", async () => {
      await renderWithItems([DRAFT_BROADCAST, SAMPLE_BROADCAST, COMPLETED_BROADCAST]);
      expect(
        screen.getByTestId(`broadcast-delete-${DRAFT_BROADCAST.id}`),
      ).toBeInTheDocument();
      expect(
        screen.queryByTestId(`broadcast-delete-${SAMPLE_BROADCAST.id}`),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByTestId(`broadcast-delete-${COMPLETED_BROADCAST.id}`),
      ).not.toBeInTheDocument();
    });

    it("deletes the draft after confirming and drops its row", async () => {
      await renderWithItems([DRAFT_BROADCAST]);
      deleteBroadcastMock.mockResolvedValue(undefined);

      fireEvent.click(screen.getByTestId(`broadcast-delete-${DRAFT_BROADCAST.id}`));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete draft" }));

      await waitFor(() =>
        expect(deleteBroadcastMock).toHaveBeenCalledWith(DRAFT_BROADCAST.id),
      );
      await waitFor(() =>
        expect(screen.queryByTestId("broadcast-row-item")).not.toBeInTheDocument(),
      );
    });

    it("does not delete when the confirm modal is cancelled", async () => {
      await renderWithItems([DRAFT_BROADCAST]);

      fireEvent.click(screen.getByTestId(`broadcast-delete-${DRAFT_BROADCAST.id}`));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      expect(deleteBroadcastMock).not.toHaveBeenCalled();
      expect(screen.getByTestId("broadcast-row-item")).toBeInTheDocument();
    });

    it("surfaces the coded copy and refreshes if the draft is no longer a draft (409)", async () => {
      await renderWithItems([DRAFT_BROADCAST]);
      deleteBroadcastMock.mockRejectedValue(
        new ApiResponseError(409, "conflict", undefined, {
          code: "broadcast_not_draft",
        }),
      );
      // The refresh after a 409 reloads the list (now showing it sending).
      listBroadcastsMock.mockResolvedValue({
        items: [{ ...DRAFT_BROADCAST, status: "sending" }],
        total: 1,
        limit: 25,
        offset: 0,
      });

      fireEvent.click(screen.getByTestId(`broadcast-delete-${DRAFT_BROADCAST.id}`));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete draft" }));

      await screen.findByText(/no longer a draft/i);
      await waitFor(() => expect(listBroadcastsMock).toHaveBeenCalledTimes(2));
    });
  });
});
