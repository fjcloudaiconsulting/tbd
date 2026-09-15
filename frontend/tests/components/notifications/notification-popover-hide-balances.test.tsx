/**
 * Notification bodies under "Hide balances" (TBD-527, fence F7).
 *
 * The body is built on the SERVER (`scheduler_cc_statement_closed`:
 * `f"{amount_str} {currency}"`, `amount_str = f"{owed:,.2f}"`), so no client
 * formatter touches it. The popover masks the amount-before-ISO-code shape.
 */
import { act, render, screen } from "@testing-library/react";

import NotificationPopover from "@/components/notifications/NotificationPopover";
import { setBalancesHidden } from "@/lib/format";
import type { Notification } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

// Byte-for-byte the backend template's in-app body.
const BODY = "Your Visa statement closed. 17,373.37 EUR is due on 2026-10-01.";

const ITEM: Notification = {
  id: 1,
  category: "cc_statement",
  event_type: "scheduler.cc_statement.closed",
  title: "Visa statement closed",
  body: BODY,
  link_url: "/accounts?edit=3",
  seen_at: null,
  read_at: null,
  audit_event_id: null,
  created_at: "2026-09-15T08:00:00",
} as unknown as Notification;

beforeEach(() => {
  setBalancesHidden(false);
});

describe("F7: notification popover body", () => {
  it("masks the server-built amount when hidden, and repaints on toggle", () => {
    render(<NotificationPopover items={[ITEM]} onAfterReadChange={() => {}} onClose={() => {}} />);
    expect(document.body.textContent).toContain("17,373.37 EUR");

    act(() => setBalancesHidden(true));
    expect(document.body.textContent).not.toMatch(/7,?373/);
    expect(screen.getByText("Your Visa statement closed. ••••• EUR is due on 2026-10-01.")).toBeInTheDocument();
  });
});
