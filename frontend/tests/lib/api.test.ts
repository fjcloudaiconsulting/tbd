import { ApiResponseError, extractErrorMessage } from "@/lib/api";


describe("extractErrorMessage", () => {
  it("returns the message for Error instances", () => {
    expect(extractErrorMessage(new Error("boom"))).toBe("boom");
  });

  it("falls back for unknown values", () => {
    expect(extractErrorMessage({ nope: true }, "fallback")).toBe("fallback");
  });

  it("preserves ApiResponseError metadata", () => {
    const error = new ApiResponseError(403, "Forbidden");

    expect(error.status).toBe(403);
    expect(extractErrorMessage(error)).toBe("Forbidden");
  });
});
