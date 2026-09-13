/**
 * SchedulerSettingsCard tests.
 *
 * Verifies the org-settings "Automatic tasks" card (Task 13):
 *   - Loads current settings via getSchedulerSettings() on mount.
 *   - Renders two labeled switches + a lead-days number input reflecting
 *     the loaded state.
 *   - Toggling a switch calls updateSchedulerSettings with ONLY the
 *     changed field.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import SchedulerSettingsCard from "@/components/settings/SchedulerSettingsCard";
import * as api from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getSchedulerSettings: vi.fn(),
    updateSchedulerSettings: vi.fn(),
  };
});

beforeEach(() => {
  vi.mocked(api.getSchedulerSettings).mockReset();
  vi.mocked(api.updateSchedulerSettings).mockReset();
  vi.mocked(api.getSchedulerSettings).mockResolvedValue({
    automate_recurring_generation: true,
    automate_billing_close: true,
    billing_close_reminder_lead_days: 3,
    automate_cc_statement_alerts: true,
    cc_statement_reminder_lead_days: 5,
  });
  vi.mocked(api.updateSchedulerSettings).mockResolvedValue({
    automate_recurring_generation: true,
    automate_billing_close: false,
    billing_close_reminder_lead_days: 3,
    automate_cc_statement_alerts: true,
    cc_statement_reminder_lead_days: 5,
  });
});

describe("SchedulerSettingsCard", () => {
  it("loads and renders current settings", async () => {
    render(<SchedulerSettingsCard />);
    await waitFor(() =>
      expect(screen.getByText(/Automatic tasks/i)).toBeInTheDocument(),
    );
    expect(
      screen.getByLabelText(/Automatically close billing period/i),
    ).toBeChecked();
    expect(
      screen.getByLabelText(/Automatically generate recurring transactions/i),
    ).toBeChecked();
    expect(
      screen.getByLabelText(/Days before a budget period closes to notify members/i),
    ).toHaveValue(3);
  });

  it("persists a toggle change", async () => {
    render(<SchedulerSettingsCard />);
    await waitFor(() =>
      screen.getByLabelText(/Automatically close billing period/i),
    );
    fireEvent.click(
      screen.getByLabelText(/Automatically close billing period/i),
    );
    await waitFor(() =>
      expect(api.updateSchedulerSettings).toHaveBeenCalledWith({
        automate_billing_close: false,
      }),
    );
  });

  it("renders a Credit-card statements sub-section with its own toggle + lead days", async () => {
    render(<SchedulerSettingsCard />);
    expect(await screen.findByText("Credit-card statement alerts")).toBeInTheDocument();
    expect(
      screen.getByLabelText(/Days before a card statement closes to remind members/i),
    ).toHaveValue(5);
  });

  it("persists the cc lead-days independently of the budget lead-days", async () => {
    render(<SchedulerSettingsCard />);
    const ccInput = await screen.findByLabelText(
      /Days before a card statement closes to remind members/i,
    );
    fireEvent.change(ccInput, { target: { value: "10" } });
    fireEvent.blur(ccInput);
    await waitFor(() =>
      expect(api.updateSchedulerSettings).toHaveBeenCalledWith({
        cc_statement_reminder_lead_days: 10,
      }),
    );
    // Pin no-cross-wiring: the budget lead-days field must be untouched by
    // the cc-input commit — no call should ever include
    // billing_close_reminder_lead_days.
    expect(api.updateSchedulerSettings).not.toHaveBeenCalledWith(
      expect.objectContaining({ billing_close_reminder_lead_days: expect.anything() }),
    );
  });

  // ── TBD-323 ────────────────────────────────────────────────────────────
  it("T8 fence: the switch name is the exact setting name and survives a real toggle on the same node", async () => {
    // Kills: an action- or state-phrased label passed at this site. The
    // regex queries above would pass "Disable automatically close billing period".
    render(<SchedulerSettingsCard />);
    const sw = await screen.findByRole("switch", { name: "Automatically close billing period" });
    expect(sw).toHaveAttribute("aria-checked", "true");
    fireEvent.click(sw);
    await waitFor(() => expect(api.updateSchedulerSettings).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByRole("switch", { name: "Automatically close billing period" })).toHaveAttribute(
        "aria-checked",
        "false",
      ),
    );
    expect(screen.getByRole("switch", { name: "Automatically close billing period" })).toBe(sw);
  });

  it("T12 fence: a second click while the save is in flight issues no second save and rollback restores the ORIGINAL value", async () => {
    // Real `disabled` was the only thing stopping a double save here. With the
    // switch kept focusable while saving, a second click would capture the
    // optimistic value as `prev` and a failure would "roll back" to it.
    let reject!: (e: unknown) => void;
    vi.mocked(api.updateSchedulerSettings).mockImplementation(
      () => new Promise((_, rej) => { reject = rej; }) as never,
    );
    render(<SchedulerSettingsCard />);
    const sw = await screen.findByRole("switch", { name: "Automatically close billing period" });
    fireEvent.click(sw);
    fireEvent.click(sw);
    fireEvent.click(sw);
    expect(api.updateSchedulerSettings).toHaveBeenCalledTimes(1);
    expect(sw).toHaveAttribute("aria-disabled", "true");
    expect(sw).not.toBeDisabled();

    reject(new Error("boom"));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "true"));
    expect(api.updateSchedulerSettings).toHaveBeenCalledTimes(1);
  });

  it("X1 fence: a sibling save finishing leaves a field still in flight pending, and it still refuses a second save", async () => {
    // Kills BOTH halves of the old single-value `savingField`: when B finished
    // it cleared A's pending state while A still saved (A's aria-disabled
    // assertion), and only the ref guard then stopped A's second save (the
    // call-count assertion).
    vi.mocked(api.updateSchedulerSettings).mockImplementation(((patch: Record<string, unknown>) =>
      "automate_billing_close" in patch
        ? new Promise(() => {})
        : Promise.resolve({})) as never);
    render(<SchedulerSettingsCard />);
    const a = await screen.findByRole("switch", { name: "Automatically close billing period" });
    const b = screen.getByRole("switch", { name: "Automatically generate recurring transactions" });

    fireEvent.click(a);
    fireEvent.click(b);
    expect(a).toHaveAttribute("aria-disabled", "true");
    await waitFor(() => expect(b).not.toHaveAttribute("aria-disabled"));
    expect(a).toHaveAttribute("aria-disabled", "true");

    fireEvent.click(a);
    const aCalls = vi
      .mocked(api.updateSchedulerSettings)
      .mock.calls.filter(([patch]) => "automate_billing_close" in (patch as object));
    expect(aCalls).toHaveLength(1);
  });

  it("X2 fence: a failed save rolls back ONLY its own field, not a sibling that saved meanwhile", async () => {
    // Kills a rollback (or optimistic write) from a stale `settings` snapshot,
    // which would put B back to the value it had when A was clicked.
    let rejectA!: (e: unknown) => void;
    vi.mocked(api.updateSchedulerSettings).mockImplementation(((patch: Record<string, unknown>) =>
      "automate_billing_close" in patch
        ? new Promise((_, rej) => { rejectA = rej; })
        : Promise.resolve({})) as never);
    render(<SchedulerSettingsCard />);
    const a = await screen.findByRole("switch", { name: "Automatically close billing period" });
    const b = screen.getByRole("switch", { name: "Automatically generate recurring transactions" });

    fireEvent.click(a);
    fireEvent.click(b);
    await waitFor(() => expect(b).not.toHaveAttribute("aria-disabled"));
    expect(b).toHaveAttribute("aria-checked", "false");

    rejectA(new Error("boom"));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await waitFor(() => expect(a).toHaveAttribute("aria-checked", "true"));
    expect(b).toHaveAttribute("aria-checked", "false");
  });

  it("X1b fence: two clicks delivered before React re-renders issue one save (the ref guard)", async () => {
    // Both clicks land inside one act() batch, so the second handler runs
    // against the PRE-render props: the switch's `pending` no-op cannot see
    // the first save yet. Only the synchronous ref guard in handleToggle can
    // refuse it. Kills: the ref guard removed (X1 alone cannot see that,
    // because between two separate fireEvent clicks React has re-rendered).
    vi.mocked(api.updateSchedulerSettings).mockImplementation((() => new Promise(() => {})) as never);
    render(<SchedulerSettingsCard />);
    const a = await screen.findByRole("switch", { name: "Automatically close billing period" });
    act(() => {
      a.click();
      a.click();
    });
    expect(api.updateSchedulerSettings).toHaveBeenCalledTimes(1);
  });
});
