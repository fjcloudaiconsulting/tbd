import { readFileSync } from "node:fs";
import path from "node:path";
import { NextRequest } from "next/server";

import { proxy } from "@/proxy";

/**
 * INF-14. public/.well-known/security.txt (RFC 9116) is served by the app
 * (Next.js public/) and copied into the apex export by build-apex.sh.
 *
 * The Expires check is a deliberate reminder: once the date passes, this
 * fails, and the fix is to set Expires about a year out again.
 */
const file = path.resolve(__dirname, "..", "public", ".well-known", "security.txt");
const body = readFileSync(file, "utf8");
const lines = body.split("\n").filter((line) => line !== "" && !line.startsWith("#"));
const values = (name: string) =>
  lines
    .filter((line) => line.toLowerCase().startsWith(`${name.toLowerCase()}: `))
    .map((line) => line.slice(name.length + 2));

describe("security.txt (RFC 9116)", () => {
  it("holds only `Field: value` lines besides comments", () => {
    for (const line of lines) expect(line).toMatch(/^[A-Za-z-]+: \S/);
  });

  it("names the SECURITY.md contact and policy", () => {
    expect(values("Contact")).toEqual(["mailto:security@thebetterdecision.com"]);
    expect(values("Policy")).toEqual([
      "https://github.com/fjcloudaiconsulting/tbd/blob/main/SECURITY.md",
    ]);
  });

  it("has exactly one Expires, in the future and at most a year out", () => {
    const expires = values("Expires");
    expect(expires).toHaveLength(1);
    expect(expires[0]).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/);
    const at = Date.parse(expires[0]);
    expect(at, "security.txt Expires has lapsed: set it about a year out").toBeGreaterThan(
      Date.now(),
    );
    expect(at).toBeLessThanOrEqual(Date.now() + 366 * 24 * 60 * 60 * 1000);
  });

  it("lists the app and apex copies as canonical", () => {
    expect(values("Canonical")).toEqual([
      "https://app.thebetterdecision.com/.well-known/security.txt",
      "https://thebetterdecision.com/.well-known/security.txt",
    ]);
  });

  it("passes through the proxy without a redirect", () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const response = proxy(
      new NextRequest("https://app.thebetterdecision.com/.well-known/security.txt", {
        headers: { host: "app.thebetterdecision.com" },
      }),
    );
    logSpy.mockRestore();
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("x-middleware-next")).toBe("1");
  });
});
