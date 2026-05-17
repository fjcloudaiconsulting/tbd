import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import ImportPage from "@/app/import/page";
import { apiFetch, ApiResponseError } from "@/lib/api";

// Mock Next.js navigation hooks (same shape as import-page.test.tsx).
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => ({ get: () => null }),
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const ACCOUNT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Bank",
  account_type_slug: "bank",
  balance: 100,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

// At least one category so the upload form (not the empty-state CTA)
// renders. Without categories the page short-circuits to the L3.10
// Layer A empty-state — we want to test the Layer B preflight error
// reaching us from the backend.
const CATEGORY_INC = {
  id: 6,
  name: "Salary",
  type: "income" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "salary",
  is_system: false,
  transaction_count: 0,
};

describe("ImportPage Layer B preflight error", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("renders targeted empty-state when backend returns missing_category_type for expense", async () => {
    const detailPayload = {
      code: "missing_category_type",
      missing_types: ["expense"],
      message:
        "This import has expense rows but you have no expense category. Add one to continue.",
    };

    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/accounts") return Promise.resolve([ACCOUNT]);
      if (url === "/api/v1/categories") return Promise.resolve([CATEGORY_INC]);
      if (url === "/api/v1/import/preview") {
        return Promise.reject(
          new ApiResponseError(
            400,
            detailPayload.message,
            "missing_category_type",
            detailPayload,
          ),
        );
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<ImportPage />);
    const uploadButton = await screen.findByRole("button", {
      name: /upload & preview/i,
    });
    const fileInput = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(["x"], "t.csv", { type: "text/csv" })] },
    });
    fireEvent.click(uploadButton);

    const node = await screen.findByTestId("missing-category-type-error");
    expect(node).toBeInTheDocument();
    // Targeted copy mentions the missing type.
    expect(node.textContent?.toLowerCase()).toContain("expense");
    // Deep-link to /categories is present.
    const link = screen.getByRole("link", { name: /go to categories/i });
    expect(link.getAttribute("href")).toBe("/categories");
  });

  it("falls back to generic banner when error is unstructured", async () => {
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/accounts") return Promise.resolve([ACCOUNT]);
      if (url === "/api/v1/categories") return Promise.resolve([CATEGORY_INC]);
      if (url === "/api/v1/import/preview") {
        return Promise.reject(new Error("network down"));
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<ImportPage />);
    const uploadButton = await screen.findByRole("button", {
      name: /upload & preview/i,
    });
    const fileInput = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(["x"], "t.csv", { type: "text/csv" })] },
    });
    fireEvent.click(uploadButton);

    await screen.findByText(/network down/i);
    // The structured empty-state must NOT appear.
    expect(
      screen.queryByTestId("missing-category-type-error"),
    ).not.toBeInTheDocument();
  });
});
