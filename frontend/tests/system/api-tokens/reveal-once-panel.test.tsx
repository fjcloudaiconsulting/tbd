import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import RevealOncePanel from "@/components/system/api-tokens/RevealOncePanel";
import type { MintTokenResponse } from "@/lib/types";

const RESULT: MintTokenResponse = {
  token: "pat_a1b2c3d4e5f6g7h8i9j0secretsecretsecret",
  id: 42,
  name: "broadcast cron",
  prefix: "pat_a1b2c3",
  scope: "read",
  created_at: "2026-07-21T12:00:00Z",
  expires_at: "2026-08-20T12:00:00Z",
};

describe("RevealOncePanel", () => {
  it("shows the full plaintext token exactly once with a won't-see-again warning", () => {
    render(<RevealOncePanel result={RESULT} onDone={vi.fn()} />);
    expect(screen.getByTestId("reveal-token")).toHaveTextContent(RESULT.token);
    expect(screen.getByTestId("reveal-panel")).toHaveTextContent(
      /you won.?t see this( token)? again/i,
    );
  });

  it("copies the token to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<RevealOncePanel result={RESULT} onDone={vi.fn()} />);
    fireEvent.click(screen.getByTestId("reveal-copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(RESULT.token));
  });

  it("calls onDone from the Done button", () => {
    const onDone = vi.fn();
    render(<RevealOncePanel result={RESULT} onDone={onDone} />);
    fireEvent.click(screen.getByTestId("reveal-done"));
    expect(onDone).toHaveBeenCalled();
  });
});
