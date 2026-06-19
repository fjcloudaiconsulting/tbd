import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import FeatureFlagsCard from "@/components/system/FeatureFlagsCard";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const FEATURES = [
  { feature: "reports", global_value: "on", env_floor: true },
  { feature: "plans", global_value: null, env_floor: false },
];

describe("FeatureFlagsCard", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("renders both feature rows with their global_value and env_floor hint", async () => {
    vi.mocked(apiFetch).mockResolvedValue(FEATURES);

    render(<FeatureFlagsCard />);

    // Loading state first.
    expect(screen.getByText(/loading feature flags/i)).toBeInTheDocument();

    // Both feature names appear.
    expect(await screen.findByText("Reports")).toBeInTheDocument();
    expect(screen.getByText("Plans")).toBeInTheDocument();

    // "Reports" row has global_value "on" — the "on" button should be pressed.
    const groups = screen.getAllByRole("group");
    const reportsGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Reports"),
    );
    expect(reportsGroup).toBeDefined();
    const onBtn = Array.from(reportsGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "on",
    );
    expect(onBtn?.getAttribute("aria-pressed")).toBe("true");

    // "Plans" row has global_value null → inherit; show env_floor hint.
    expect(
      screen.getByText(/inheriting environment default/i),
    ).toBeInTheDocument();
    // env_floor=false → "off" shown in the hint.
    expect(screen.getByText(/inheriting environment default/i).textContent).toMatch(/off/i);

    // The "inherit" button in the Plans group is pressed.
    const plansGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Plans"),
    );
    expect(plansGroup).toBeDefined();
    const inheritBtn = Array.from(plansGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "inherit",
    );
    expect(inheritBtn?.getAttribute("aria-pressed")).toBe("true");
  });

  it("PUTs the chosen value and updates the row on change", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT" && typeof url === "string" && url.includes("/reports")) {
        return { feature: "reports", global_value: "off", env_floor: true };
      }
      return FEATURES;
    });

    render(<FeatureFlagsCard />);

    // Wait for data to load.
    expect(await screen.findByText("Reports")).toBeInTheDocument();

    // Click "off" in the Reports group.
    const groups = screen.getAllByRole("group");
    const reportsGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Reports"),
    );
    expect(reportsGroup).toBeDefined();

    const offBtn = Array.from(reportsGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "off",
    );
    expect(offBtn).toBeDefined();
    fireEvent.click(offBtn!);

    // PUT was fired with { value: "off" }.
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls;
      const putCall = calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url === "/api/v1/admin/features/reports" &&
          (opts as RequestInit | undefined)?.method === "PUT",
      );
      expect(putCall).toBeDefined();
      const body = JSON.parse(String((putCall![1] as RequestInit).body));
      expect(body).toEqual({ value: "off" });
    });

    // Row now reflects the updated value ("off" button is pressed).
    await waitFor(() => {
      const updatedGroups = screen.getAllByRole("group");
      const updatedReportsGroup = updatedGroups.find(
        (g) => g.getAttribute("aria-label")?.includes("Reports"),
      );
      const updatedOffBtn = Array.from(
        updatedReportsGroup!.querySelectorAll("button"),
      ).find((b) => b.textContent === "off");
      expect(updatedOffBtn?.getAttribute("aria-pressed")).toBe("true");
    });

    // Success indicator is shown.
    await waitFor(() => {
      expect(screen.getByText("Saved")).toBeInTheDocument();
    });
  });

  it("shows an error message when the initial fetch fails", async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error("network error"));

    render(<FeatureFlagsCard />);

    await waitFor(() => {
      expect(screen.getByText(/network error/i)).toBeInTheDocument();
    });
  });

  it("shows a per-feature error when PUT fails", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        throw new Error("forbidden");
      }
      return FEATURES;
    });

    render(<FeatureFlagsCard />);
    expect(await screen.findByText("Reports")).toBeInTheDocument();

    const groups = screen.getAllByRole("group");
    const reportsGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Reports"),
    );
    const offBtn = Array.from(reportsGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "off",
    );
    fireEvent.click(offBtn!);

    await waitFor(() => {
      expect(screen.getByText(/forbidden/i)).toBeInTheDocument();
    });
  });
});
