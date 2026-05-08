import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import AdjustBalanceModal from "@/components/accounts/AdjustBalanceModal";
import { apiFetch, ApiResponseError } from "@/lib/api";
import type { Account } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const ACCOUNT: Account = {
  id: 10,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Bank",
  account_type_slug: "checking",
  balance: 100 as unknown as Account["balance"],
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

describe("AdjustBalanceModal", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("posts a negative-delta adjustment and calls onAdjusted on success", async () => {
    const onClose = vi.fn();
    const onAdjusted = vi.fn();
    vi.mocked(apiFetch).mockResolvedValueOnce({
      account_id: 10,
      old_balance: 100,
      new_balance: 70,
      delta: -30,
      transaction_id: 555,
    });

    render(
      <AdjustBalanceModal
        account={ACCOUNT}
        onClose={onClose}
        onAdjusted={onAdjusted}
      />
    );

    const input = screen.getByLabelText(/Target balance/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "70" } });
    fireEvent.click(screen.getByRole("button", { name: /Apply adjustment/i }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledTimes(1);
    });
    const [path, options] = vi.mocked(apiFetch).mock.calls[0]!;
    expect(path).toBe("/api/v1/accounts/10/adjust-balance");
    expect(options?.method).toBe("POST");
    const body = JSON.parse(String(options?.body));
    expect(body.target_balance).toBe(70);
    expect(body.reason).toBeNull();

    await waitFor(() => {
      expect(onAdjusted).toHaveBeenCalledTimes(1);
    });
  });

  it("surfaces a 409 'No change to apply' error inline without calling onAdjusted", async () => {
    const onClose = vi.fn();
    const onAdjusted = vi.fn();
    vi.mocked(apiFetch).mockRejectedValueOnce(
      new ApiResponseError(409, "No change to apply", { detail: "No change to apply" })
    );

    render(
      <AdjustBalanceModal
        account={ACCOUNT}
        onClose={onClose}
        onAdjusted={onAdjusted}
      />
    );

    const input = screen.getByLabelText(/Target balance/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: /Apply adjustment/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/No change to apply/);
    });
    expect(onAdjusted).not.toHaveBeenCalled();
  });

  it("cancels without firing the API or calling onAdjusted", async () => {
    const onClose = vi.fn();
    const onAdjusted = vi.fn();

    render(
      <AdjustBalanceModal
        account={ACCOUNT}
        onClose={onClose}
        onAdjusted={onAdjusted}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(apiFetch).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onAdjusted).not.toHaveBeenCalled();
  });
});
