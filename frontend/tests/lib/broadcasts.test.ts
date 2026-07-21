import { describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "@/lib/api";
import {
  BROADCAST_ERROR_COPY,
  broadcastErrorCode,
  createBroadcast,
  listRecipients,
} from "@/lib/broadcasts";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});

describe("broadcastErrorCode", () => {
  it("reads the code from err.detail.code (Ruling R7.1), not err.code", () => {
    const err = new ApiResponseError(422, "x", undefined, { code: "dry_run_required" });
    expect(broadcastErrorCode(err)).toBe("dry_run_required");
  });

  it("returns undefined for a plain Error", () => {
    expect(broadcastErrorCode(new Error("boom"))).toBeUndefined();
  });

  it("returns undefined for an ApiResponseError with no detail", () => {
    const err = new ApiResponseError(500, "x");
    expect(broadcastErrorCode(err)).toBeUndefined();
  });

  it("returns undefined when detail is a string, not an object", () => {
    const err = new ApiResponseError(422, "x", undefined, "not json");
    expect(broadcastErrorCode(err)).toBeUndefined();
  });
});

describe("BROADCAST_ERROR_COPY", () => {
  it("has a non-empty string for invalid_template_token", () => {
    expect(typeof BROADCAST_ERROR_COPY.invalid_template_token).toBe("string");
    expect(BROADCAST_ERROR_COPY.invalid_template_token.length).toBeGreaterThan(0);
  });

  it("covers all six coded errors", () => {
    for (const code of [
      "dry_run_required",
      "confirm_subject_mismatch",
      "confirm_count_mismatch",
      "recipient_cap_exceeded",
      "broadcast_not_draft",
      "invalid_template_token",
    ]) {
      expect(BROADCAST_ERROR_COPY[code]).toBeTruthy();
    }
  });
});

describe("api wrappers", () => {
  it("createBroadcast hardcodes segment=active_verified (Ruling R7.2)", async () => {
    const { apiFetch } = await import("@/lib/api");
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 1 });

    await createBroadcast("Hello", "Body {first_name}");

    expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/admin/broadcasts",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          subject: "Hello",
          body_template: "Body {first_name}",
          segment: "active_verified",
        }),
      }),
    );
  });

  it("listRecipients converts page/pageSize into limit/offset", async () => {
    const { apiFetch } = await import("@/lib/api");
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [],
      total: 0,
      limit: 25,
      offset: 25,
    });

    await listRecipients(7, 1, 25);

    expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/admin/broadcasts/7/recipients?limit=25&offset=25",
    );
  });
});
