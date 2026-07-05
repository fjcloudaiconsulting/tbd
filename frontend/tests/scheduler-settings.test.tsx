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
  });
  vi.mocked(api.updateSchedulerSettings).mockResolvedValue({
    automate_recurring_generation: true,
    automate_billing_close: false,
    billing_close_reminder_lead_days: 3,
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
      screen.getByLabelText(/Days before close to notify members/i),
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
});
