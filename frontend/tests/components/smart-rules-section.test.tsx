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
});
