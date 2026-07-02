import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import OrgFeatureGateCard from "@/components/admin/OrgFeatureGateCard";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const GATES = [
  { feature: "reports", override: "inherit", effective: true },
  { feature: "plans", override: "off", effective: false },
  { feature: "custom_dashboard", override: "inherit", effective: true },
];

describe("OrgFeatureGateCard", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("renders both feature rows with override state and effective indicator", async () => {
    vi.mocked(apiFetch).mockResolvedValue(GATES);

    render(<OrgFeatureGateCard orgId={42} />);

    // Loading state first.
    expect(screen.getByText(/loading feature gates/i)).toBeInTheDocument();

    // Feature names appear with their friendly labels (not raw keys).
    expect(await screen.findByText("Reports")).toBeInTheDocument();
    expect(screen.getByText("Plans")).toBeInTheDocument();
    expect(screen.getByText("Customizable dashboard")).toBeInTheDocument();
    expect(screen.queryByText("custom_dashboard")).not.toBeInTheDocument();

    // "Reports" row has override "inherit" — the "inherit" button should be pressed.
    const groups = screen.getAllByRole("group");
    const reportsGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Reports"),
    );
    expect(reportsGroup).toBeDefined();
    const inheritBtn = Array.from(reportsGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "inherit",
    );
    expect(inheritBtn?.getAttribute("aria-pressed")).toBe("true");

    // "Reports" effective: true → "Enabled" shown.
    const rows = screen.getAllByText(/Effective:/i);
    // Reports row (first): effective=true
    expect(rows[0].parentElement?.textContent).toMatch(/Enabled/);
    // Plans row (second): effective=false
    expect(rows[1].parentElement?.textContent).toMatch(/Disabled/);

    // "Plans" row has override "off" — the "off" button should be pressed.
    const plansGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Plans"),
    );
    expect(plansGroup).toBeDefined();
    const offBtn = Array.from(plansGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "off",
    );
    expect(offBtn?.getAttribute("aria-pressed")).toBe("true");
  });

  it("PUTs {value:'on'} when Reports is set to on and updates row from response", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT" && typeof url === "string" && url.includes("/reports")) {
        return { feature: "reports", override: "on", effective: true };
      }
      return GATES;
    });

    render(<OrgFeatureGateCard orgId={42} />);

    // Wait for data to load.
    expect(await screen.findByText("Reports")).toBeInTheDocument();

    // Click "on" in the Reports group.
    const groups = screen.getAllByRole("group");
    const reportsGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Reports"),
    );
    expect(reportsGroup).toBeDefined();

    const onBtn = Array.from(reportsGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "on",
    );
    expect(onBtn).toBeDefined();
    fireEvent.click(onBtn!);

    // PUT was fired with { value: "on" } to the correct URL.
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls;
      const putCall = calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url === "/api/v1/admin/orgs/42/features/reports" &&
          (opts as RequestInit | undefined)?.method === "PUT",
      );
      expect(putCall).toBeDefined();
      const body = JSON.parse(String((putCall![1] as RequestInit).body));
      expect(body).toEqual({ value: "on" });
    });

    // Row now reflects the server-confirmed value ("on" button is pressed).
    await waitFor(() => {
      const updatedGroups = screen.getAllByRole("group");
      const updatedReportsGroup = updatedGroups.find(
        (g) => g.getAttribute("aria-label")?.includes("Reports"),
      );
      const updatedOnBtn = Array.from(
        updatedReportsGroup!.querySelectorAll("button"),
      ).find((b) => b.textContent === "on");
      expect(updatedOnBtn?.getAttribute("aria-pressed")).toBe("true");
    });

    // Success indicator is shown.
    await waitFor(() => {
      expect(screen.getByText("Saved")).toBeInTheDocument();
    });
  });

  it("PUTs {value:'inherit'} when inherit is selected", async () => {
    // Start with override "off" for reports so we can click "inherit".
    const gatesOff = [
      { feature: "reports", override: "off", effective: false },
      { feature: "plans", override: "off", effective: false },
    ];
    vi.mocked(apiFetch).mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT" && typeof url === "string" && url.includes("/reports")) {
        return { feature: "reports", override: "inherit", effective: true };
      }
      return gatesOff;
    });

    render(<OrgFeatureGateCard orgId={42} />);
    expect(await screen.findByText("Reports")).toBeInTheDocument();

    const groups = screen.getAllByRole("group");
    const reportsGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Reports"),
    );
    const inheritBtn = Array.from(reportsGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "inherit",
    );
    expect(inheritBtn).toBeDefined();
    fireEvent.click(inheritBtn!);

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls;
      const putCall = calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url === "/api/v1/admin/orgs/42/features/reports" &&
          (opts as RequestInit | undefined)?.method === "PUT",
      );
      expect(putCall).toBeDefined();
      const body = JSON.parse(String((putCall![1] as RequestInit).body));
      expect(body).toEqual({ value: "inherit" });
    });
  });

  it("shows an error message when the initial fetch fails", async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error("network error"));

    render(<OrgFeatureGateCard orgId={42} />);

    await waitFor(() => {
      expect(screen.getByText(/network error/i)).toBeInTheDocument();
    });
  });

  it("shows a per-feature error when PUT fails", async () => {
    vi.mocked(apiFetch).mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        throw new Error("forbidden");
      }
      return GATES;
    });

    render(<OrgFeatureGateCard orgId={42} />);
    expect(await screen.findByText("Reports")).toBeInTheDocument();

    const groups = screen.getAllByRole("group");
    const reportsGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Reports"),
    );
    // Reports has override "inherit"; click "on" to trigger a change.
    const onBtn = Array.from(reportsGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "on",
    );
    fireEvent.click(onBtn!);

    await waitFor(() => {
      expect(screen.getByText(/forbidden/i)).toBeInTheDocument();
    });
  });

  it("does not fire PUT when clicking the already-selected option", async () => {
    vi.mocked(apiFetch).mockResolvedValue(GATES);

    render(<OrgFeatureGateCard orgId={42} />);
    expect(await screen.findByText("Reports")).toBeInTheDocument();

    const groups = screen.getAllByRole("group");
    const reportsGroup = groups.find(
      (g) => g.getAttribute("aria-label")?.includes("Reports"),
    );
    // Reports has override "inherit"; click "inherit" again — no PUT should fire.
    const inheritBtn = Array.from(reportsGroup!.querySelectorAll("button")).find(
      (b) => b.textContent === "inherit",
    );
    fireEvent.click(inheritBtn!);

    await waitFor(() => {
      const putCalls = vi.mocked(apiFetch).mock.calls.filter(
        ([, opts]) => (opts as RequestInit | undefined)?.method === "PUT",
      );
      expect(putCalls).toHaveLength(0);
    });
  });
});
