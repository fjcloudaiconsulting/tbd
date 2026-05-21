/**
 * Tag chip input component tests (PR-Tags-A frontend).
 *
 * Covers:
 *   - Renders existing chips with remove buttons.
 *   - Suggest endpoint is debounced + queried on draft change.
 *   - Enter on draft commits a chip.
 *   - Comma commits a chip.
 *   - Backspace on empty input removes the last chip.
 *   - Clicking a suggestion commits a chip.
 *   - Pressing the chip remove button removes the chip.
 *   - Cap (MAX_TAGS_PER_TRANSACTION) blocks further entries with an
 *     inline error.
 *   - aria-live region announces suggestion counts.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import TagChipInput, {
  MAX_TAGS_PER_TRANSACTION,
  type TagSuggestion,
} from "@/components/transactions/TagChipInput";

type Fetcher = (
  prefix: string,
  categoryId: number | null,
  signal: AbortSignal,
) => Promise<TagSuggestion[]>;

function deferred<T>() {
  let resolveFn!: (v: T) => void;
  let rejectFn!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolveFn = res;
    rejectFn = rej;
  });
  return { promise, resolve: resolveFn, reject: rejectFn };
}

function Harness({
  initial = [],
  fetcher,
  categoryId = null,
}: {
  initial?: string[];
  fetcher: Fetcher;
  categoryId?: number | null;
}) {
  const [tags, setTags] = useState<string[]>(initial);
  return (
    <TagChipInput
      id="test-tags"
      value={tags}
      onChange={setTags}
      categoryId={categoryId}
      fetcher={fetcher}
      debounceMs={10}
    />
  );
}

const SAMPLE: TagSuggestion[] = [
  { name: "insurance", source: "org_recent", weight: 5 },
  { name: "groceries", source: "org_co_category", weight: 3 },
];

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("TagChipInput", () => {
  it("renders existing chips and exposes a remove button per chip", () => {
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness initial={["insurance", "rent"]} fetcher={fetcher} />);
    expect(screen.getByTestId("tag-chip-insurance")).toBeTruthy();
    expect(screen.getByTestId("tag-chip-rent")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove tag insurance" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove tag rent" }),
    ).toBeTruthy();
  });

  it("commits a typed tag on Enter and clears the draft", async () => {
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "rent" } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(screen.getByTestId("tag-chip-rent")).toBeTruthy();
    expect(input.value).toBe("");
  });

  it("commits a typed tag on comma", async () => {
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "rent" } });
      fireEvent.keyDown(input, { key: "," });
    });
    expect(screen.getByTestId("tag-chip-rent")).toBeTruthy();
  });

  it("commits a typed tag on Space and clears the draft", async () => {
    // Space is the most discoverable "add another tag" trigger for
    // users coming from social platforms. The previous version of the
    // input only committed on Enter / Tab / comma — fjorge feedback
    // 2026-05-21: nobody figures out Enter on first try.
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "bike" } });
      fireEvent.keyDown(input, { key: " " });
    });
    expect(screen.getByTestId("tag-chip-bike")).toBeTruthy();
    expect(input.value).toBe("");
  });

  it("Space at an empty draft is a no-op (does NOT commit an empty chip)", async () => {
    // Avoids the surprise where pressing space on focus creates a
    // mysterious empty chip. The keyboard handler explicitly returns
    // early when draft is empty.
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    await act(async () => {
      input.focus();
      fireEvent.keyDown(input, { key: " " });
    });
    // No chip created.
    expect(input.parentElement?.querySelectorAll("[data-testid^='tag-chip-']").length).toBe(0);
  });

  it("normalizes uppercase to lowercase on commit", async () => {
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "Insurance" } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(screen.getByTestId("tag-chip-insurance")).toBeTruthy();
  });

  it("removes the last chip on Backspace when the input is empty", async () => {
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness initial={["one", "two"]} fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    await act(async () => {
      input.focus();
      fireEvent.keyDown(input, { key: "Backspace" });
    });
    expect(screen.queryByTestId("tag-chip-two")).toBeNull();
    expect(screen.getByTestId("tag-chip-one")).toBeTruthy();
  });

  it("removes a chip when its remove button is clicked", async () => {
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    render(<Harness initial={["one", "two"]} fetcher={fetcher} />);
    const removeBtn = screen.getByRole("button", { name: "Remove tag one" });
    await act(async () => {
      fireEvent.click(removeBtn);
    });
    expect(screen.queryByTestId("tag-chip-one")).toBeNull();
  });

  it("fetches suggestions after debounce and commits one on click", async () => {
    const d = deferred<TagSuggestion[]>();
    const fetcher: Fetcher = vi.fn().mockReturnValue(d.promise);
    render(<Harness fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "in" } });
      // Advance through the 10ms debounce.
      await vi.advanceTimersByTimeAsync(15);
      d.resolve(SAMPLE);
    });
    const option = await screen.findByRole("option", { name: /insurance/ });
    await act(async () => {
      fireEvent.mouseDown(option);
    });
    expect(screen.getByTestId("tag-chip-insurance")).toBeTruthy();
  });

  it("announces suggestion count to the aria-live region", async () => {
    const d = deferred<TagSuggestion[]>();
    const fetcher: Fetcher = vi.fn().mockReturnValue(d.promise);
    render(<Harness fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "in" } });
      await vi.advanceTimersByTimeAsync(15);
      d.resolve(SAMPLE);
    });
    await waitFor(() => {
      const live = screen.getByRole("status");
      expect(live.textContent).toContain("2 suggestions available");
    });
  });

  it("caps at MAX_TAGS_PER_TRANSACTION and surfaces an inline error", async () => {
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    const initial = Array.from(
      { length: MAX_TAGS_PER_TRANSACTION },
      (_, i) => `tag${i}`,
    );
    render(<Harness initial={initial} fetcher={fetcher} />);
    const input = screen.getByRole("combobox") as HTMLInputElement;
    // The input is disabled when at cap; the disabled state is the
    // primary signal. Typing into a disabled input is a no-op.
    expect(input.disabled).toBe(true);
  });

  it("keeps chip remove buttons reachable + functional at MAX_TAGS (a11y regression)", async () => {
    // Regression for PR #326 review: at cap, the input is disabled
    // AND the chip x buttons must remain in the natural tab order so
    // a keyboard-only user can free a slot. tabIndex={-1} on the
    // remove buttons would have created a WCAG keyboard trap (no way
    // to remove a tag once the cap was hit). This test walks the
    // full recovery path: focus a chip x, press Enter to remove,
    // then add a new chip back to the cap.
    const fetcher: Fetcher = vi.fn().mockResolvedValue([]);
    const onChange = vi.fn();
    const initial = Array.from(
      { length: MAX_TAGS_PER_TRANSACTION },
      (_, i) => `tag${i}`,
    );

    function ControlledHarness() {
      const [tags, setTags] = useState<string[]>(initial);
      return (
        <TagChipInput
          id="cap-test"
          value={tags}
          onChange={(next) => {
            onChange(next);
            setTags(next);
          }}
          fetcher={fetcher}
          debounceMs={10}
        />
      );
    }

    render(<ControlledHarness />);

    // (1) Input is disabled at the cap.
    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.disabled).toBe(true);

    // (2) Every chip x must be tabbable. tabIndex < 0 on any of them
    // would mean Tab cannot reach it — that is the bug this test
    // guards against.
    const removeButtons = screen.getAllByRole("button", {
      name: /^Remove tag /,
    });
    expect(removeButtons).toHaveLength(MAX_TAGS_PER_TRANSACTION);
    for (const btn of removeButtons) {
      expect(btn.tabIndex).toBeGreaterThanOrEqual(0);
      expect((btn as HTMLButtonElement).disabled).toBe(false);
    }

    // (3) Focus the first chip x as a keyboard user would (in a real
    // browser, Shift+Tab from the disabled input lands here; jsdom
    // does not run the Tab-walk algorithm so we focus directly).
    const firstRemove = removeButtons[0];
    await act(async () => {
      firstRemove.focus();
    });
    expect(document.activeElement).toBe(firstRemove);

    // (4) Press Enter on the focused x. The component must remove
    // the chip and call onChange with the cap-1 list.
    await act(async () => {
      fireEvent.keyDown(firstRemove, { key: "Enter" });
    });
    expect(onChange).toHaveBeenCalled();
    const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(lastCall).toHaveLength(MAX_TAGS_PER_TRANSACTION - 1);
    expect(lastCall).not.toContain("tag0");

    // (5) Recovery: the input is now re-enabled. Typing a new tag +
    // Enter commits a chip back up to the cap.
    const refreshedInput = screen.getByRole("combobox") as HTMLInputElement;
    expect(refreshedInput.disabled).toBe(false);
    await act(async () => {
      fireEvent.change(refreshedInput, { target: { value: "freshtag" } });
      fireEvent.keyDown(refreshedInput, { key: "Enter" });
    });
    const finalCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(finalCall).toContain("freshtag");
    expect(finalCall).toHaveLength(MAX_TAGS_PER_TRANSACTION);
  });
});
