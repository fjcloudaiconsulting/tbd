import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";

// One image for all envs (INFRA-40): NEXT_PUBLIC_* is inlined at build time, so
// it must not exist anywhere config is declared. Use lib/runtime-config.ts.
const repoRoot = path.resolve(__dirname, "../../..");
const SELF = "frontend/tests/convention/runtime-config-only.test.ts";
const SCOPE = [
  "frontend",
  ".do",
  ".github",
  ".env.example",
  "docker-compose.yml",
  "docker-compose.prod.yml",
];

describe("runtime-only frontend config", () => {
  it("no NEXT_PUBLIC_* in code, Dockerfiles, compose, CI or the DO spec", () => {
    const files = execFileSync("git", ["ls-files", ...SCOPE], { cwd: repoRoot, encoding: "utf8" })
      .split("\n")
      .filter((f) => f && f !== SELF && !/\.(png|ico|jpe?g|woff2?|svg|lock)$|package-lock\.json$/.test(f));
    expect(files.length).toBeGreaterThan(50);
    const offenders = files.filter((f) => readFileSync(path.join(repoRoot, f), "utf8").includes("NEXT_PUBLIC_"));
    expect(offenders).toEqual([]);
  });
});
