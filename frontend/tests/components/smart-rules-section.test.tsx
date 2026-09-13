import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SmartRulesSection from "@/components/settings/SmartRulesSection";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

describe("SmartRulesSection", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("loads the current value (off) and reflects it in the switch", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/settings") {
        return Promise.resolve([
          { key: "session_lifetime_days", value: "30" },
          { key: "share_merchant_data", value: "false" },
        ]);
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<SmartRulesSection />);

    await waitFor(() => {
      expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
    });
  });

  it("loads the current value (on) when key is 'true'", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/settings") {
        return Promise.resolve([{ key: "share_merchant_data", value: "true" }]);
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<SmartRulesSection />);

    await waitFor(() => {
      expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
    });
  });

  it("defaults to off when the key is absent from settings", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/settings") {
        return Promise.resolve([]);
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<SmartRulesSection />);

    await waitFor(() => {
      expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
    });
  });

  it("PUTs the setting on click and flips the visible state", async () => {
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === "/api/v1/settings" && (!opts || opts.method !== "PUT")) {
        return Promise.resolve([]);
      }
      if (url === "/api/v1/settings" && opts?.method === "PUT") {
        return Promise.resolve({ key: "share_merchant_data", value: "true" });
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<SmartRulesSection />);

    await waitFor(() => {
      expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
    });

    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => {
      expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
    });

    expect(apiFetchMock).toHaveBeenLastCalledWith(
      "/api/v1/settings",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ key: "share_merchant_data", value: "true" }),
      }),
    );
  });

  // ── TBD-323 ────────────────────────────────────────────────────────────
  it("T9 fence: the name is exactly the visible label (WCAG 2.5.3) and survives a toggle", async () => {
    // Kills: the pre-TBD-323 name "Share merchant data", which did not contain
    // the visible label, and any state-phrased name.
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (opts?.method === "PUT") return Promise.resolve({ key: "share_merchant_data", value: "true" });
      if (url === "/api/v1/settings") return Promise.resolve([]);
      return Promise.resolve(undefined);
    }) as never);
    render(<SmartRulesSection />);
    const sw = await screen.findByRole("switch", { name: "Share anonymized merchant data" });
    expect(screen.getByText("Share anonymized merchant data")).toBeInTheDocument();
    fireEvent.click(sw);
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "true"));
    expect(screen.getByRole("switch", { name: "Share anonymized merchant data" })).toBe(sw);
  });

  it("T15 fence: no switch and no state claim until the value has been read", async () => {
    // Kills: rendering the default `false` as "Disabled" before the GET resolves.
    let resolve!: (v: unknown) => void;
    apiFetchMock.mockImplementation((() => new Promise((r) => { resolve = r; })) as never);
    render(<SmartRulesSection />);
    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByText("Disabled")).toBeNull();
    resolve([{ key: "share_merchant_data", value: "true" }]);
    expect(await screen.findByRole("switch", { name: "Share anonymized merchant data" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.queryByText("Loading...")).toBeNull();
  });

  it("T16 fence: a failed load shows an alert and no switch", async () => {
    // Kills: the swallowed catch that left a "Disabled" switch asserting a
    // value that was never read.
    apiFetchMock.mockImplementation((() => Promise.reject(new Error("network down"))) as never);
    render(<SmartRulesSection />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByText("Disabled")).toBeNull();
    expect(screen.queryByText("Loading...")).toBeNull();
  });

  it("T17 fence: a second click while the save is in flight issues one PUT", async () => {
    let resolvePut!: (v: unknown) => void;
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (opts?.method === "PUT") return new Promise((r) => { resolvePut = r; });
      if (url === "/api/v1/settings") return Promise.resolve([]);
      return Promise.resolve(undefined);
    }) as never);
    render(<SmartRulesSection />);
    const sw = await screen.findByRole("switch", { name: "Share anonymized merchant data" });
    fireEvent.click(sw);
    fireEvent.click(sw);
    const puts = () => apiFetchMock.mock.calls.filter(([, o]) => (o as RequestInit | undefined)?.method === "PUT");
    expect(puts()).toHaveLength(1);
    expect(sw).not.toBeDisabled();
    resolvePut({ key: "share_merchant_data", value: "true" });
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "true"));
    expect(puts()).toHaveLength(1);
  });
});
