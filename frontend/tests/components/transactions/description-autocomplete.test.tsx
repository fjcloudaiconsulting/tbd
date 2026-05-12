/**
 * Description-autocomplete component tests (L3.2 Wave 2A).
 *
 * Covers:
 * - Debounce: typing fewer than the debounce window does not fire.
 * - Min-query-length: q < 2 chars never triggers a fetch.
 * - Keyboard nav: ArrowDown / ArrowUp / Enter / Escape.
 * - ARIA wiring: combobox/listbox/option roles, aria-activedescendant,
 *   aria-expanded.
 * - onPick callback fires with the picked suggestion.
 * - Live region announces result counts politely.
 * - AbortController invalidates in-flight requests when the user drops
 *   below the 2-char minimum, so stale responses cannot re-open the
 *   dropdown (P1 regression from the owner review of PR #239).
 */
import { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DescriptionAutocomplete, {
  type DescriptionSuggestion,
} from "@/components/transactions/DescriptionAutocomplete";

type Fetcher = (
  type: "income" | "expense" | "transfer",
  q: string,
  signal: AbortSignal,
) => Promise<DescriptionSuggestion[]>;

/** Hand-controlled fetcher: each call returns a Deferred you can
 *  resolve when you want, and the AbortSignal each call received so
 *  tests can assert cancellation. */
function makeDeferredFetcher() {
  const calls: {
    type: "income" | "expense" | "transfer";
    q: string;
    signal: AbortSignal;
    resolve: (list: DescriptionSuggestion[]) => void;
    reject: (err: unknown) => void;
  }[] = [];
  const fetcher: Fetcher = (type, q, signal) => {
    return new Promise<DescriptionSuggestion[]>((resolve, reject) => {
      calls.push({ type, q, signal, resolve, reject });
      // When the host aborts, reject with a DOMException("AbortError")
      // to match the real fetch() behavior the component handles.
      signal.addEventListener("abort", () => {
        reject(new DOMException("The user aborted a request.", "AbortError"));
      });
    });
  };
  return { fetcher, calls };
}

const SAMPLE: DescriptionSuggestion[] = [
  {
    description: "Albert Heijn",
    category_id: 5,
    category_name: "Groceries",
    use_count: 12,
    last_used: "2026-05-10",
  },
  {
    description: "Albert Cuyp",
    category_id: 5,
    category_name: "Groceries",
    use_count: 4,
    last_used: "2026-05-05",
  },
];

function Harness({
  initial = "",
  fetcher,
  onPick,
}: {
  initial?: string;
  fetcher: Fetcher;
  onPick?: (s: DescriptionSuggestion) => void;
}) {
  const [v, setV] = useState(initial);
  return (
    <DescriptionAutocomplete
      id="tx-desc-test"
      type="expense"
      value={v}
      onChange={setV}
      onPick={onPick}
      fetcher={fetcher}
      debounceMs={20}
    />
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("DescriptionAutocomplete", () => {
  it("renders the combobox with the correct ARIA attributes", () => {
    const fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness fetcher={fetcher} />);
    const cb = screen.getByRole("combobox");
    expect(cb).toHaveAttribute("aria-autocomplete", "list");
    expect(cb).toHaveAttribute("aria-expanded", "false");
    expect(cb).toHaveAttribute("aria-controls");
  });

  it("does not fetch when query length is below 2 chars", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(SAMPLE);
    render(<Harness fetcher={fetcher} />);
    const cb = screen.getByRole("combobox");
    fireEvent.change(cb, { target: { value: "a" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("debounces fetches and pops the listbox open with results", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(SAMPLE);
    render(<Harness fetcher={fetcher} />);
    const cb = screen.getByRole("combobox");
    fireEvent.change(cb, { target: { value: "Al" } });
    expect(fetcher).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    expect(fetcher).toHaveBeenCalledWith(
      "expense",
      "Al",
      expect.any(AbortSignal),
    );
    await waitFor(() =>
      expect(screen.getByRole("listbox")).toBeInTheDocument(),
    );
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(2);
    expect(options[0]).toHaveTextContent("Albert Heijn");
    expect(options[1]).toHaveTextContent("Albert Cuyp");
  });

  it("supports ArrowDown / ArrowUp / Enter keyboard navigation", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(SAMPLE);
    const onPick = vi.fn();
    render(<Harness fetcher={fetcher} onPick={onPick} />);
    const cb = screen.getByRole("combobox");
    fireEvent.change(cb, { target: { value: "Al" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() =>
      expect(screen.getByRole("listbox")).toBeInTheDocument(),
    );

    // Default highlight is index 0 after fetch resolves.
    expect(cb).toHaveAttribute("aria-activedescendant");

    fireEvent.keyDown(cb, { key: "ArrowDown" });
    fireEvent.keyDown(cb, { key: "Enter" });

    expect(onPick).toHaveBeenCalledWith(SAMPLE[1]);
    // After picking, the input's value should be the picked description.
    expect((cb as HTMLInputElement).value).toBe("Albert Cuyp");
    // And the listbox should be closed.
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes the listbox on Escape", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(SAMPLE);
    render(<Harness fetcher={fetcher} />);
    const cb = screen.getByRole("combobox");
    fireEvent.change(cb, { target: { value: "Al" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() =>
      expect(screen.getByRole("listbox")).toBeInTheDocument(),
    );
    fireEvent.keyDown(cb, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("announces result count via polite live region", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(SAMPLE);
    render(<Harness fetcher={fetcher} />);
    const cb = screen.getByRole("combobox");
    fireEvent.change(cb, { target: { value: "Al" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /2 suggestions available/,
      ),
    );
  });

  it("clicking a suggestion selects it", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(SAMPLE);
    const onPick = vi.fn();
    render(<Harness fetcher={fetcher} onPick={onPick} />);
    const cb = screen.getByRole("combobox");
    fireEvent.change(cb, { target: { value: "Al" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    const options = await screen.findAllByRole("option");
    fireEvent.mouseDown(options[0]);
    expect(onPick).toHaveBeenCalledWith(SAMPLE[0]);
    expect((cb as HTMLInputElement).value).toBe("Albert Heijn");
  });

  it("aborts the in-flight request when the user keeps typing", async () => {
    const { fetcher, calls } = makeDeferredFetcher();
    render(<Harness fetcher={fetcher} />);
    const cb = screen.getByRole("combobox");
    // Fire request #1.
    fireEvent.change(cb, { target: { value: "Al" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() => expect(calls).toHaveLength(1));
    // Mutate input before #1 resolves so a fresh #2 is queued.
    fireEvent.change(cb, { target: { value: "Albe" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() => expect(calls).toHaveLength(2));
    // Request #1 should be aborted now; #2 still pending.
    expect(calls[0].signal.aborted).toBe(true);
    expect(calls[1].signal.aborted).toBe(false);
    // Even if the cancelled request "resolves" with stale data, it
    // must not commit. Resolve #1 with stale data and #2 with the
    // fresh result.
    await act(async () => {
      calls[0].resolve([SAMPLE[0]]); // stale; should be ignored
      calls[1].resolve([SAMPLE[1]]); // fresh; this is what renders
    });
    await waitFor(() =>
      expect(screen.getByRole("listbox")).toBeInTheDocument(),
    );
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent("Albert Cuyp");
  });

  // ── P1 regression: cleared query must not show stale suggestions ──
  //
  // Owner review of PR #239: when the user types ≥2 chars, fetches, then
  // clears the input back below the 2-char minimum, the in-flight fetch
  // must be cancelled. If it resolves anyway, the dropdown must NOT
  // reopen with the stale results.

  it("does not show stale suggestions after the query is cleared below the 2-char minimum", async () => {
    const { fetcher, calls } = makeDeferredFetcher();
    render(<Harness fetcher={fetcher} />);
    const cb = screen.getByRole("combobox");
    // 1. Type 3 chars → debounce → fetch in flight.
    fireEvent.change(cb, { target: { value: "Alb" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].signal.aborted).toBe(false);
    // 2. Backspace all 3 chars → query is now "" (below 2-char min).
    fireEvent.change(cb, { target: { value: "" } });
    // The effect cleanup runs synchronously on the next render, so by
    // the time advanceTimers fires, the in-flight fetch is aborted.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    // 3. Stale fetch is cancelled; signal must be aborted.
    expect(calls[0].signal.aborted).toBe(true);
    // 4. Try to apply the stale response anyway. The component must
    //    drop it on the floor because the signal is aborted.
    await act(async () => {
      calls[0].resolve([SAMPLE[0], SAMPLE[1]]);
    });
    // 5. Listbox must NOT reopen, no stale items in the DOM.
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryByText("Albert Heijn")).not.toBeInTheDocument();
    expect(screen.queryByText("Albert Cuyp")).not.toBeInTheDocument();
  });

  it("renders only the latest fetch's results across rapid type/backspace cycles", async () => {
    // Variation: Alb → Alb (no-op typing pause) → backspace → Alb,
    // resolve the older fetch, assert only the latest fetch wins.
    const { fetcher, calls } = makeDeferredFetcher();
    render(<Harness fetcher={fetcher} />);
    const cb = screen.getByRole("combobox");
    fireEvent.change(cb, { target: { value: "Alb" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() => expect(calls).toHaveLength(1));
    // User backspaces one char (still 2 chars, "Al") then re-adds
    // "b" so the value is "Alb" again. Each re-render of the effect
    // cleans up the previous controller and starts a new fetch.
    fireEvent.change(cb, { target: { value: "Al" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    fireEvent.change(cb, { target: { value: "Alb" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    // The first (and any intermediate) call must be aborted; the last
    // call's signal must still be alive.
    for (let i = 0; i < calls.length - 1; i++) {
      expect(calls[i].signal.aborted).toBe(true);
    }
    expect(calls[calls.length - 1].signal.aborted).toBe(false);
    // Resolving the stale calls with bogus data must not render.
    await act(async () => {
      for (let i = 0; i < calls.length - 1; i++) {
        calls[i].resolve([SAMPLE[0]]);
      }
      calls[calls.length - 1].resolve([SAMPLE[1]]);
    });
    await waitFor(() =>
      expect(screen.getByRole("listbox")).toBeInTheDocument(),
    );
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent("Albert Cuyp");
  });
});
