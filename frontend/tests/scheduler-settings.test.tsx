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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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
});
