/**
 * C2b — drag-and-drop subcategory move on /categories.
 *
 * Strategy:
 *   - Unit-test the pure classifier (`classifyDrop`) and error
 *     formatter (`buildMoveErrorMessage`) against every meaningful
 *     drop combination + every backend error code we care about.
 *   - Render-level tests assert the Edit-mode gate (drag handles +
 *     drop zones + the instructions banner only appear in Edit mode)
 *     and that Batch Move stays fully wired as the SR-accessible
 *     fallback.
 *
 * Real pointer-driven drag simulation in jsdom is intentionally
 * skipped: dnd-kit's sensors depend on layout measurements jsdom
 * cannot provide. The contract is dnd-kit's responsibility; ours is
 * the handler logic and the Edit-mode gate, both of which are pure.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, beforeEach, it, expect, vi } from "vitest";
import * as React from "react";

import CategoriesPage from "@/app/categories/page";
import DragMoveConfirmModal from "@/components/categories/DragMoveConfirmModal";
import { apiFetch, ApiResponseError } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import {
  buildMoveErrorMessage,
  classifyDrop,
} from "@/components/categories/dragMoveHelpers";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/categories",
}));

const USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const CATEGORIES = [
  {
    id: 100,
    name: "Food",
    slug: "food_dining",
    parent_id: null,
    parent_name: null,
    type: "expense" as const,
    is_system: true,
    description: null,
    transaction_count: 0,
  },
  {
    id: 101,
    name: "Restaurants",
    slug: null,
    parent_id: 100,
    parent_name: "Food",
    type: "expense" as const,
    is_system: false,
    description: null,
    transaction_count: 5,
  },
  {
    id: 200,
    name: "Lifestyle",
    slug: "lifestyle",
    parent_id: null,
    parent_name: null,
    type: "expense" as const,
    is_system: true,
    description: null,
    transaction_count: 0,
  },
  {
    id: 300,
    name: "Income",
    slug: "income",
    parent_id: null,
    parent_name: null,
    type: "income" as const,
    is_system: true,
    description: null,
    transaction_count: 0,
  },
];

function setupApi(handlers: Record<string, (init?: RequestInit) => unknown> = {}) {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    if (url === "/api/v1/categories" && (!init || init.method === undefined)) {
      return Promise.resolve(CATEGORIES);
    }
    if (handlers[url]) {
      const result = handlers[url](init);
      return result instanceof Promise ? result : Promise.resolve(result);
    }
    const noQuery = url.split("?")[0];
    if (handlers[noQuery]) {
      const result = handlers[noQuery](init);
      return result instanceof Promise ? result : Promise.resolve(result);
    }
    return Promise.resolve({});
  }) as never);
}

// ---------------------------------------------------------------------
// classifyDrop — pure unit tests for the drag-end handler core.
// ---------------------------------------------------------------------

describe("classifyDrop", () => {
  const subData = {
    kind: "subcategory" as const,
    subcategoryId: 101,
    subcategoryName: "Restaurants",
    subcategoryType: "expense" as const,
    parentId: 100,
  };

  it("returns 'valid' for a same-type cross-master drop", () => {
    const result = classifyDrop(subData, {
      kind: "master",
      masterId: 200,
      masterType: "expense",
    });
    expect(result.kind).toBe("valid");
    if (result.kind === "valid") {
      expect(result.target.masterId).toBe(200);
      expect(result.sub.subcategoryId).toBe(101);
    }
  });

  it("returns 'source_parent' when dropping on the current parent (no API call)", () => {
    const result = classifyDrop(subData, {
      kind: "master",
      masterId: 100,
      masterType: "expense",
    });
    expect(result.kind).toBe("source_parent");
  });

  it("returns 'cross_type' when dropping an expense sub on an income master", () => {
    const result = classifyDrop(subData, {
      kind: "master",
      masterId: 300,
      masterType: "income",
    });
    expect(result.kind).toBe("cross_type");
  });

  it("returns 'no_drop' when the drag did not land over a droppable", () => {
    expect(classifyDrop(subData, undefined).kind).toBe("no_drop");
    expect(classifyDrop(subData, null).kind).toBe("no_drop");
  });

  it("returns 'wrong_kind' when active is not a subcategory", () => {
    const result = classifyDrop(
      { kind: "master", masterId: 100, masterType: "expense" },
      { kind: "master", masterId: 200, masterType: "expense" },
    );
    expect(result.kind).toBe("wrong_kind");
  });

  it("returns 'wrong_kind' when over is not a master", () => {
    const result = classifyDrop(subData, {
      kind: "subcategory",
      subcategoryId: 999,
      subcategoryName: "Other",
      subcategoryType: "expense",
      parentId: 200,
    });
    expect(result.kind).toBe("wrong_kind");
  });

  it("rejects malformed active payloads", () => {
    expect(classifyDrop({ kind: "subcategory" }, { kind: "master", masterId: 1, masterType: "expense" }).kind).toBe("wrong_kind");
  });

  it("rejects malformed over payloads", () => {
    expect(classifyDrop(subData, { kind: "master" }).kind).toBe("wrong_kind");
  });

  it("'both' source type only matches a 'both' master", () => {
    const bothSub = { ...subData, subcategoryType: "both" as const };
    expect(
      classifyDrop(bothSub, { kind: "master", masterId: 200, masterType: "expense" }).kind,
    ).toBe("cross_type");
    expect(
      classifyDrop(bothSub, { kind: "master", masterId: 200, masterType: "both" }).kind,
    ).toBe("valid");
  });
});

// ---------------------------------------------------------------------
// buildMoveErrorMessage — pure unit tests for the error formatter.
// ---------------------------------------------------------------------

describe("buildMoveErrorMessage", () => {
  it("formats a structured name_collision 409 detail", () => {
    const err = new ApiResponseError(409, "name_collision", undefined, {
      detail: "name_collision",
      target_parent_id: 200,
      conflicting_child_id: 201,
      conflicting_child_name: "Restaurants",
      normalized_name: "restaurants",
    });
    const msg = buildMoveErrorMessage(err, "Restaurants", "Lifestyle");
    expect(msg).toContain("Restaurants");
    expect(msg).toContain("Lifestyle");
    expect(msg).toContain("already exists");
  });

  it("formats a structured type_mismatch 400 detail distinctly from name_collision", () => {
    const err = new ApiResponseError(400, "type_mismatch", undefined, {
      detail: "type_mismatch",
      source_type: "expense",
      target_type: "income",
      dependent_breakdown: { income: 0, expense: 3 },
    });
    const msg = buildMoveErrorMessage(err, "Restaurants", "Income");
    expect(msg).toContain("incompatible");
    expect(msg).not.toContain("already exists");
  });

  it("falls back to err.message for unknown detail strings", () => {
    const err = new ApiResponseError(500, "boom", undefined, { detail: "oops" });
    const msg = buildMoveErrorMessage(err, "Restaurants", "Lifestyle");
    expect(msg).toContain("oops");
  });

  it("returns a sensible fallback for non-Error values", () => {
    const msg = buildMoveErrorMessage("plain string", "Restaurants", "Lifestyle");
    expect(msg).toContain("Restaurants");
  });
});

// ---------------------------------------------------------------------
// Render-level: Edit-mode gate, sticky Batch Move presence.
// ---------------------------------------------------------------------

describe("CategoriesPage -C2b render-level gates", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    setupApi();
  });

  it("does not render drag handles or the instructions banner outside Edit mode", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    expect(screen.queryByTestId("sub-drag-handle-101")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("categories-drag-instructions"),
    ).not.toBeInTheDocument();
  });

  it("entering Edit mode adds drag handles on subcategory rows ONLY", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    expect(screen.getByTestId("sub-drag-handle-101")).toBeInTheDocument();
    expect(screen.getByTestId("categories-drag-instructions")).toBeInTheDocument();
    // Masters never get a drag handle.
    expect(screen.queryByTestId("sub-drag-handle-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sub-drag-handle-200")).not.toBeInTheDocument();
  });

  it("master rows render as drop zones in Edit mode", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    expect(screen.getByTestId("master-dropzone-100")).toBeInTheDocument();
    expect(screen.getByTestId("master-dropzone-200")).toBeInTheDocument();
    expect(screen.getByTestId("master-dropzone-300")).toBeInTheDocument();

    // Initial state: no active drag, every dropzone neutral.
    for (const id of [100, 200, 300]) {
      const zone = screen.getByTestId(`master-dropzone-${id}`);
      expect(zone.getAttribute("data-drop-valid")).toBe("false");
      expect(zone.getAttribute("data-drop-invalid")).toBe("false");
    }
  });

  it("exiting Edit mode clears drag handles and drop zones", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    expect(screen.getByTestId("sub-drag-handle-101")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    expect(screen.queryByTestId("sub-drag-handle-101")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("categories-drag-instructions"),
    ).not.toBeInTheDocument();
  });

  it("Batch Move remains the keyboard/SR-accessible fallback in Edit mode", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    fireEvent.click(screen.getByTestId("sub-checkbox-101"));

    expect(screen.getByTestId("batch-action-bar")).toBeInTheDocument();
    expect(screen.getByTestId("batch-move-button")).toBeInTheDocument();
    expect(screen.getByTestId("batch-delete-button")).toBeInTheDocument();
  });

  it("the drag handle is a real, focusable button with an aria-label", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    const handle = screen.getByTestId("sub-drag-handle-101");
    // dnd-kit assigns role=button + tabindex=0 so keyboard users can
    // start a drag with the keyboard. Aria-label scopes it to the
    // subcategory name so screen readers announce the target row.
    expect(handle.getAttribute("aria-label")).toContain("Restaurants");
    expect(handle.getAttribute("role") ?? handle.tagName.toLowerCase()).toMatch(/button/i);
  });

  it("no /move or /move/preview API call fires on mount or Edit toggle", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    const urls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes("/move/preview"))).toBe(false);
    expect(urls.some((u) => /\/move(\?|$)/.test(u))).toBe(false);
  });
});

// ---------------------------------------------------------------------
// DragMoveConfirmModal — modal a11y contract.
//
// Asserts the modal wires the focus-trap / Escape / focus-restore /
// scroll-lock pattern the remediation wave (PRs #203-#209)
// standardized. Tests render the modal directly so we don't have to
// drive a real drag to inspect its a11y behavior.
// ---------------------------------------------------------------------

describe("DragMoveConfirmModal -modal a11y", () => {
  const NOOP = () => {};
  const PREVIEW = {
    affected_transaction_count: 5,
    affected_recurring_count: 1,
    affected_forecast_item_count: 2,
    budget_actuals_shifted: false,
  };

  it("returns null and does not lock body scroll when closed", () => {
    document.body.style.overflow = "";
    const { container } = render(
      <DragMoveConfirmModal
        open={false}
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={null}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(document.body.style.overflow).toBe("");
  });

  it("renders with role=dialog + aria-modal when open and locks body scroll", () => {
    document.body.style.overflow = "";
    render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-labelledby")).toBe("drag-move-confirm-title");
    expect(document.body.style.overflow).toBe("hidden");
  });

  it("moves focus to the Cancel button on open (avoids accidental confirm)", async () => {
    render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    await waitFor(() => {
      const cancel = screen.getByRole("button", { name: "Cancel" });
      expect(document.activeElement).toBe(cancel);
    });
  });

  it("Escape calls onCancel", () => {
    const onCancel = vi.fn();
    render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={onCancel}
      />,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("clicking the backdrop calls onCancel; clicking inside the dialog does not", () => {
    const onCancel = vi.fn();
    render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={onCancel}
      />,
    );
    // Click inside the dialog body — should not bubble to backdrop.
    fireEvent.click(screen.getByRole("dialog"));
    expect(onCancel).not.toHaveBeenCalled();

    // Click on the backdrop wrapper (the testid container is the
    // backdrop because it's the outer .fixed inset-0).
    fireEvent.click(screen.getByTestId("drag-move-confirm"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("restores body scroll on unmount", () => {
    document.body.style.overflow = "";
    const { unmount } = render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    expect(document.body.style.overflow).toBe("hidden");
    unmount();
    expect(document.body.style.overflow).toBe("");
  });

  it("restores focus to the previously-focused element after close", async () => {
    const prior = document.createElement("button");
    prior.setAttribute("data-testid", "prior-focus");
    prior.textContent = "prior";
    document.body.appendChild(prior);
    prior.focus();
    expect(document.activeElement).toBe(prior);

    const { rerender } = render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    // Focus moved into the dialog.
    await waitFor(() => {
      expect(document.activeElement).not.toBe(prior);
    });

    // Close.
    rerender(
      <DragMoveConfirmModal
        open={false}
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    await waitFor(() => {
      expect(document.activeElement).toBe(prior);
    });

    document.body.removeChild(prior);
  });

  it("Tab key is captured by the focus trap (defaultPrevented) when at a trap edge", () => {
    // jsdom's `offsetParent === null` filter inside the focus-trap
    // hook makes the wrap-around assertion unreliable in a unit test
    // (every focusable element looks 'hidden' to the hook). What we
    // can prove deterministically is that the hook is installed: a
    // Tab keydown is observed by the document-level listener while
    // the modal is open. The wrap-around itself is covered by the
    // same `useFocusTrap` hook tests that ship with the rest of the
    // modal family.
    render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    const cancel = screen.getByRole("button", { name: "Cancel" });
    cancel.focus();
    const ev = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    document.dispatchEvent(ev);
    // The hook calls preventDefault when there are no real focusable
    // matches (jsdom) OR when at a trap edge (real browser). Either
    // way, the trap is engaged.
    expect(ev.defaultPrevented).toBe(true);
  });

  it("disables Confirm while preview is loading or absent", () => {
    const { rerender } = render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={null}
        previewLoading
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    expect(screen.getByRole("button", { name: "Move" })).toBeDisabled();

    rerender(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError=""
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    expect(screen.getByRole("button", { name: "Move" })).not.toBeDisabled();
  });

  it("surfaces moveError in an alert region without dismissing the modal", () => {
    render(
      <DragMoveConfirmModal
        open
        subcategoryName="Restaurants"
        targetMasterName="Lifestyle"
        preview={PREVIEW}
        previewLoading={false}
        previewError=""
        moveError="Cannot move: name collision."
        submitting={false}
        onConfirm={NOOP}
        onCancel={NOOP}
      />,
    );
    const err = screen.getByTestId("drag-move-error");
    expect(err.getAttribute("role")).toBe("alert");
    expect(err.textContent).toContain("name collision");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------
// Touch target — the drag handle must meet 44x44px on mobile (Edit
// mode supports TouchSensor and the row is the only drag activator).
// md+ may shrink to a more compact size for desktop density.
// ---------------------------------------------------------------------

describe("DraggableSubcategoryRow -touch target", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    setupApi();
  });

  it("drag handle has min-h-[44px] / min-w-[44px] for mobile touch", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    const handle = screen.getByTestId("sub-drag-handle-101");
    const classes = handle.className;
    expect(classes).toContain("min-h-[44px]");
    expect(classes).toContain("min-w-[44px]");
    // Compact density allowed at md+.
    expect(classes).toMatch(/md:min-h-8/);
    expect(classes).toMatch(/md:min-w-6/);
  });

  it("drag handle icon is aria-hidden (the button itself owns the label)", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    const handle = screen.getByTestId("sub-drag-handle-101");
    const svg = handle.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute("aria-hidden")).toBe("true");
  });
});
