import { GET } from "@/app/health/route";

describe("GET /health", () => {
  it("responds 200 with {status:'ok'} for the App Platform probe", async () => {
    const response = GET();

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ status: "ok" });
  });
});
