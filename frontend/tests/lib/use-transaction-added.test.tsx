import { act, render } from "@testing-library/react";

import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";

function Probe({ onEvent }: { onEvent: () => void }) {
  useTransactionAddedListener(onEvent);
  return null;
}

describe("useTransactionAddedListener", () => {
  it("invokes the supplied reload when pfv:transaction-added fires", () => {
    const reload = vi.fn();
    render(<Probe onEvent={reload} />);
    expect(reload).not.toHaveBeenCalled();

    act(() => {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    });

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("removes the listener on unmount so a stale closure cannot fire", () => {
    const reload = vi.fn();
    const { unmount } = render(<Probe onEvent={reload} />);
    unmount();

    act(() => {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    });

    expect(reload).not.toHaveBeenCalled();
  });

  it("invokes the latest reload identity even when the prop changes between renders", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Probe onEvent={first} />);

    rerender(<Probe onEvent={second} />);

    act(() => {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    });

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
