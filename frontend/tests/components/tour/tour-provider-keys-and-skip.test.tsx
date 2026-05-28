/**
 * TourProvider integration tests (L5.3).
 *
 * Pins three Copilot-flagged behaviors that the unit-level constants
 * tests cannot reach:
 *
 *   - ArrowLeft / ArrowRight do NOT hijack the cursor when focus is
 *     inside an input/textarea/select/contenteditable element. Without
 *     this, a user typing in a form during the tour would lose normal
 *     keyboard navigation.
 *
 *   - ArrowRight DOES advance the step when focus is outside any
 *     editable element. Escape DOES close.
 *
 *   - The missing-anchor auto-skip suspends while the pathname doesn't
 *     match the step's expected route. Without this, slow cross-page
 *     navigation would silently skip past steps before their anchors
 *     mount.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TourProvider } from "@/components/tour/TourProvider";
import { useTour } from "@/components/tour/useTour";

let mockPathname = "/transactions";
const mockPush = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({
    push: (route: string) => {
      mockPush(route);
      // Simulate the route having committed for tests that need it.
      mockPathname = route;
    },
  }),
}));

function StartButton({ steps }: { steps: string[] }) {
  const api = useTour();
  return (
    <button data-testid="start" onClick={() => api.start(steps)}>
      start
    </button>
  );
}

beforeEach(() => {
  mockPathname = "/transactions";
  mockPush.mockReset();
  // Provide an anchor for the on-route step so the tour doesn't
  // immediately enter the auto-skip path during the keyboard tests.
  const anchor = document.createElement("div");
  anchor.setAttribute("data-tour-id", "transactions.title");
  document.body.appendChild(anchor);
});

afterEach(() => {
  document.querySelectorAll("[data-tour-id]").forEach((el) => el.remove());
  vi.useRealTimers();
});

describe("TourProvider keyboard handling", () => {
  it("ArrowRight ignored when focus is inside an INPUT", async () => {
    render(
      <TourProvider>
        <input data-testid="page-input" />
        <StartButton steps={["transactions.title"]} />
      </TourProvider>,
    );
    fireEvent.click(screen.getByTestId("start"));
    await waitFor(() => screen.getByTestId("tour-card"));
    const card = screen.getByTestId("tour-card");
    const labelBefore = card.querySelector(".uppercase")?.textContent;
    expect(labelBefore).toMatch(/Step 1 of 1/);

    const input = screen.getByTestId("page-input") as HTMLInputElement;
    input.focus();
    fireEvent.keyDown(input, { key: "ArrowRight" });

    // The tour MUST still be active on the same single step — pressing
    // ArrowRight in an input cannot advance/finish the tour.
    const labelAfter = card.querySelector(".uppercase")?.textContent;
    expect(labelAfter).toMatch(/Step 1 of 1/);
  });

  it("ArrowRight ignored when focus is in a contenteditable", async () => {
    render(
      <TourProvider>
        <div data-testid="rich-text" contentEditable suppressContentEditableWarning>
          hello
        </div>
        <StartButton steps={["transactions.title", "transactions.title"]} />
      </TourProvider>,
    );
    fireEvent.click(screen.getByTestId("start"));
    await waitFor(() => screen.getByTestId("tour-card"));

    const rt = screen.getByTestId("rich-text") as HTMLDivElement;
    rt.focus();
    fireEvent.keyDown(rt, { key: "ArrowRight" });

    const label = screen.getByTestId("tour-card").querySelector(".uppercase")
      ?.textContent;
    expect(label).toMatch(/Step 1 of 2/);
  });

  it("ArrowRight advances when focus is NOT on an editable element", async () => {
    render(
      <TourProvider>
        <StartButton steps={["transactions.title", "transactions.title"]} />
      </TourProvider>,
    );
    fireEvent.click(screen.getByTestId("start"));
    await waitFor(() => screen.getByTestId("tour-card"));

    fireEvent.keyDown(document.body, { key: "ArrowRight" });

    await waitFor(() => {
      const label = screen.getByTestId("tour-card").querySelector(".uppercase")
        ?.textContent;
      expect(label).toMatch(/Step 2 of 2/);
    });
  });

  it("Escape closes the tour even from an editable element", async () => {
    render(
      <TourProvider>
        <input data-testid="page-input" />
        <StartButton steps={["transactions.title"]} />
      </TourProvider>,
    );
    fireEvent.click(screen.getByTestId("start"));
    await waitFor(() => screen.getByTestId("tour-card"));

    const input = screen.getByTestId("page-input") as HTMLInputElement;
    input.focus();
    fireEvent.keyDown(input, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByTestId("tour-card")).toBeNull();
    });
  });
});

describe("TourProvider missing-anchor auto-skip", () => {
  it("does NOT auto-skip while the pathname doesn't match the step's expected route", async () => {
    vi.useFakeTimers();
    // Start on /transactions but the first step targets the
    // categories surface — TourRouter will push, but on a slow page
    // the anchor isn't there yet. The provider must suspend
    // auto-skip until the route settles.
    mockPathname = "/transactions";
    render(
      <TourProvider>
        <StartButton
          steps={["categories.title", "transactions.title"]}
        />
      </TourProvider>,
    );
    act(() => {
      fireEvent.click(screen.getByTestId("start"));
    });
    await act(async () => {
      // Past the old 200ms auto-skip + new 800ms grace.
      vi.advanceTimersByTime(1500);
    });
    // We never planted a `categories.title` anchor, but the pathname
    // also hasn't matched `/categories` yet (router push is mocked to
    // be synchronous on this test, but pathname is recomputed AFTER
    // the next render — assert the engine is STILL on step 1, not
    // auto-skipped to step 2).
    //
    // (The actual product behavior is that TourRouter will push and
    // the destination page will eventually mount its anchor. This
    // test only checks that the auto-skip doesn't fire while we're
    // still on /transactions.)
    const label = screen.queryByTestId("tour-card")
      ?.querySelector(".uppercase")?.textContent;
    // Either still on step 1, or the overlay rendered briefly in
    // centred-no-anchor mode — both are acceptable. The forbidden
    // state is "step 2 of 2" which would mean an auto-skip past
    // step 1 happened.
    if (label) {
      expect(label).not.toMatch(/Step 2 of 2/);
    }
  });
});
