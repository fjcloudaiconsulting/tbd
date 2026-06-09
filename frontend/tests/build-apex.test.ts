import { afterEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

// build-apex.test.ts — static assertions about the apex build target
// that do not require actually running `next build` in the test process.
//
// What's covered here without a real build:
//   - Allowlist contents of scripts/build-apex.sh (vs app/ on disk).
//   - next.config.apex.ts exports `output: 'export'` and the auth-island
//     + AuthProvider webpack aliases.
//   - package.json has the build:apex script wired to the bash driver.
//   - lib/links.ts returns relative paths when NEXT_PUBLIC_BUILD_TARGET
//     is unset and absolute BRAND_APP_URL paths when set to "apex".
//
// We intentionally do NOT shell out to `next build` here — it takes ~20s
// and the integration is owned by CI.

const frontendDir = path.resolve(__dirname, "..");

function readText(rel: string): string {
  return readFileSync(path.join(frontendDir, rel), "utf-8");
}

describe("apex build target — scripts/build-apex.sh", () => {
  const script = readText("scripts/build-apex.sh");

  it("uses strict bash flags (set -euo pipefail)", () => {
    expect(script).toContain("set -euo pipefail");
  });

  it("traps EXIT / INT / TERM to restore the app/ tree", () => {
    expect(script).toMatch(/trap\s+restore_routes\s+EXIT\s+INT\s+TERM/);
  });

  it("invokes next build with BUILD_TARGET=apex env", () => {
    expect(script).toContain("NEXT_PUBLIC_BUILD_TARGET=apex");
    expect(script).toMatch(/npx\s+next\s+build/);
  });

  it("swaps next.config.ts <-> next.config.apex.ts (Next 16 has no --config flag)", () => {
    expect(script).toContain("next.config.apex.ts");
    expect(script).toContain("next.config.ts");
    // Restoration path must include the backup move.
    expect(script).toContain("CONFIG_BACKUP");
  });

  it("emits _meta.json with commit + build_at + target=apex", () => {
    expect(script).toContain("_meta.json");
    expect(script).toContain('"target": "apex"');
    expect(script).toMatch(/git[^\n]+rev-parse HEAD/);
  });

  it("uses a positive allowlist of route directories (default-deny)", () => {
    // The apex build must invert the historical blocklist to an allowlist
    // so newly added authed routes (e.g. PR #238's /onboarding) are
    // staged out automatically. The script defines an ALLOWED_ROUTE_DIRS
    // array and walks app/ entries, staging anything not on it.
    expect(script).toMatch(/ALLOWED_ROUTE_DIRS=\(/);
    // Only the public marketing routes belong on the apex.
    for (const allowed of ["privacy", "terms", "docs"]) {
      expect(script, `expected allowed route: ${allowed}`).toContain(
        `"${allowed}"`,
      );
    }
    // The blocklist contract is gone — these names must NOT appear as
    // first-class array entries any more (they're staged-out via the
    // default-deny walk).
    expect(script).not.toMatch(/EXCLUDED_ROUTE_DIRS=/);
  });

  it("uses a positive allowlist of app-level files", () => {
    expect(script).toMatch(/ALLOWED_APP_FILES=\(/);
    // Structural / static files we keep.
    for (const allowed of [
      "layout.tsx",
      "page.tsx",
      "globals.css",
      "error.tsx",
      "not-found.tsx",
      "loading.tsx",
      "global-error.tsx",
      "icon.svg",
    ]) {
      expect(script, `expected allowed app file: ${allowed}`).toContain(
        `"${allowed}"`,
      );
    }
    // Dynamic Metadata handlers + dynamic image responses are NOT on the
    // allowlist (staged out so output:'export' doesn't choke).
    for (const denied of [
      "opengraph-image.tsx",
      "apple-icon.tsx",
      "sitemap.ts",
      "robots.ts",
    ]) {
      // These names may still appear in comments, but not as quoted
      // allowlist entries. The allowlist itself must not contain them.
      const allowlistBlock = script.match(
        /ALLOWED_APP_FILES=\(([\s\S]*?)\)/,
      );
      expect(allowlistBlock, "ALLOWED_APP_FILES block exists").not.toBeNull();
      expect(
        allowlistBlock?.[1] ?? "",
        `${denied} must NOT be in ALLOWED_APP_FILES`,
      ).not.toContain(`"${denied}"`);
    }
  });

  it("allowlists the apex-exported output paths", () => {
    // The post-build guard's allowlist of out-apex/ top-level entries.
    for (const allowed of [
      "index.html",
      "privacy",
      "terms",
      "docs",
      "_next",
      "_meta.json",
      // Static social-share image copied from public/og.png. Must be on
      // the output allowlist or the post-build guard rejects it.
      "og.png",
      // Static llms.txt copied from public/llms.txt. Must be on the
      // output allowlist or the post-build guard rejects it.
      "llms.txt",
    ]) {
      expect(script).toContain(`"${allowed}"`);
    }
  });

  it("welcomes the major AI crawlers in the apex robots.txt", () => {
    // The apex hosts only public marketing + docs content, so the
    // generated robots.txt explicitly allows the major training /
    // live-retrieval bots in addition to the catch-all User-agent: *.
    for (const bot of [
      "GPTBot",
      "ChatGPT-User",
      "OAI-SearchBot",
      "ClaudeBot",
      "Claude-Web",
      "anthropic-ai",
      "PerplexityBot",
      "Google-Extended",
    ]) {
      expect(script, `expected AI crawler group: ${bot}`).toContain(
        `User-agent: ${bot}`,
      );
    }
    // The catch-all + sitemap pointer must survive.
    expect(script).toContain("User-agent: *");
    expect(script).toMatch(/Sitemap: \$\{APEX_URL\}\/sitemap\.xml/);
  });

  it("lists /docs/plans in the apex sitemap", () => {
    // /docs/plans ships to the apex (it lives under the allowlisted docs/
    // route dir) and canonicalizes to the apex, so the apex sitemap must
    // list it alongside the other public routes.
    expect(script).toContain("/docs/plans/</loc>");
  });

  it("post-build guard fails on unexpected top-level entries", () => {
    // The guard exists, references the allowlist, and exits non-zero on
    // mismatch. This is belt-and-suspenders behind the input allowlist.
    expect(script).toMatch(/post-build guard/);
    expect(script).toMatch(/ALLOWED_OUTPUT_GLOBS=\(/);
    expect(script).toMatch(/output_allowed/);
  });

  it("post-build guard fails on any /api/v1 reference in built output", () => {
    // If auth/backend code (or a hashed _next/static chunk) leaks an
    // /api/v1 reference, the script must abort. This catches transitive
    // imports the input allowlist alone cannot.
    expect(script).toMatch(/grep -rl "\/api\/v1"/);
    expect(script).toMatch(/GUARD FAIL .*api\/v1/i);
  });

  it("avoids Bash 4+ features so macOS /bin/bash (3.2) can run it", () => {
    // The project is operated from macOS where /bin/bash is Bash 3.2.57.
    // None of these idioms exist in 3.2 and must not appear in code
    // (mentions inside comments are filtered out below).
    const codeOnly = script
      .split("\n")
      .filter((line) => !line.trim().startsWith("#"))
      .join("\n");
    expect(codeOnly).not.toMatch(/\bdeclare\s+-A\b/);
    expect(codeOnly).not.toMatch(/\bmapfile\b/);
    expect(codeOnly).not.toMatch(/\breadarray\b/);
    expect(codeOnly).not.toMatch(/\$\{[A-Za-z_][A-Za-z0-9_]*\^\^?\}/);
    expect(codeOnly).not.toMatch(/\$\{[A-Za-z_][A-Za-z0-9_]*,,?\}/);
    expect(codeOnly).not.toMatch(/&>>/);
  });
});

describe("apex build allowlist covers new marketing routes", () => {
  const script = readText("scripts/build-apex.sh");
  it.each(["features", "compare", "vs"])("ALLOWED_ROUTE_DIRS includes %s", (d) => {
    expect(script).toMatch(new RegExp(`ALLOWED_ROUTE_DIRS=\\([^)]*"${d}"`, "s"));
  });
  it.each(["features", "compare", "vs"])("ALLOWED_OUTPUT_GLOBS includes %s", (d) => {
    expect(script).toMatch(new RegExp(`ALLOWED_OUTPUT_GLOBS=\\([^)]*"${d}"`, "s"));
  });
  it.each(["/features/", "/compare/", "/vs/spreadsheets/", "/vs/ynab/"])(
    "apex sitemap heredoc lists %s",
    (route) => {
      expect(script).toContain(`${"${APEX_URL}"}${route}</loc>`);
    },
  );
});

describe("apex build target — next.config.apex.ts", () => {
  const config = readText("next.config.apex.ts");

  it("sets output: 'export' (static export)", () => {
    expect(config).toMatch(/output:\s*["']export["']/);
  });

  it("sets trailingSlash: true and unoptimized images for S3 / CloudFront", () => {
    expect(config).toMatch(/trailingSlash:\s*true/);
    expect(config).toMatch(/unoptimized:\s*true/);
  });

  it("does NOT set a headers() block (CloudFront owns response headers)", () => {
    // The standalone next.config.ts wires CSP + HSTS etc. via headers().
    // `output: 'export'` ignores that contract; we must not silently set
    // it here to avoid the false impression that it does anything.
    expect(config).not.toMatch(/async\s+headers\s*\(/);
  });

  it("aliases the auth-island to the no-op apex stub", () => {
    expect(config).toContain("@/components/landing/LandingAuthRedirect");
    expect(config).toContain("LandingAuthRedirectApex.tsx");
  });

  it("aliases AuthProvider to the no-op apex stub", () => {
    expect(config).toContain("@/components/auth/AuthProvider");
    expect(config).toContain("AuthProviderApex.tsx");
  });

  it("writes to a separate distDir so the standard build is undisturbed", () => {
    expect(config).toMatch(/distDir:\s*["']\.next-apex["']/);
  });
});

describe("apex build target — package.json scripts", () => {
  const pkg = JSON.parse(readText("package.json")) as {
    scripts: Record<string, string>;
  };

  it("exposes build:apex that delegates to the bash driver", () => {
    expect(pkg.scripts["build:apex"]).toBeDefined();
    expect(pkg.scripts["build:apex"]).toContain("scripts/build-apex.sh");
  });

  it("exposes start:apex-preview that serves out-apex on a known port", () => {
    expect(pkg.scripts["start:apex-preview"]).toBeDefined();
    expect(pkg.scripts["start:apex-preview"]).toContain("out-apex");
  });

  it("keeps the standard build / start scripts intact", () => {
    expect(pkg.scripts.build).toBe("next build");
    expect(pkg.scripts.start).toBe("next start");
  });
});

describe("apex build target — apex stubs", () => {
  it("LandingAuthRedirectApex exists and is a default-exported component", () => {
    const stub = readText("components/landing/LandingAuthRedirectApex.tsx");
    expect(stub).toContain("export default function LandingAuthRedirectApex");
    // No useAuth, no useRouter — must be auth-free.
    expect(stub).not.toContain("useAuth");
    expect(stub).not.toContain("useRouter");
  });

  it("AuthProviderApex exposes useAuth + AuthProvider + MfaRequiredError", () => {
    const stub = readText("components/auth/AuthProviderApex.tsx");
    expect(stub).toContain("export function AuthProvider");
    expect(stub).toContain("export function useAuth");
    expect(stub).toContain("MfaRequiredError");
    // The whole point of this stub is to NOT call apiFetch.
    expect(stub).not.toContain("apiFetch");
  });
});

// lib/links.ts reads NEXT_PUBLIC_BUILD_TARGET at module evaluation time.
// vi.resetModules() forces a fresh evaluation per test case so each env
// permutation is independent.
async function loadLinks(env: { target?: string; appUrl?: string }) {
  delete process.env.NEXT_PUBLIC_BUILD_TARGET;
  delete process.env.NEXT_PUBLIC_APP_URL;
  if (env.target !== undefined) {
    process.env.NEXT_PUBLIC_BUILD_TARGET = env.target;
  }
  if (env.appUrl !== undefined) {
    process.env.NEXT_PUBLIC_APP_URL = env.appUrl;
  }
  vi.resetModules();
  return (await import("@/lib/links")) as typeof import("@/lib/links");
}

describe("apex build target — lib/links.ts cross-domain CTAs", () => {
  const origTarget = process.env.NEXT_PUBLIC_BUILD_TARGET;
  const origUrl = process.env.NEXT_PUBLIC_APP_URL;

  afterEach(() => {
    if (origTarget === undefined) {
      delete process.env.NEXT_PUBLIC_BUILD_TARGET;
    } else {
      process.env.NEXT_PUBLIC_BUILD_TARGET = origTarget;
    }
    if (origUrl === undefined) {
      delete process.env.NEXT_PUBLIC_APP_URL;
    } else {
      process.env.NEXT_PUBLIC_APP_URL = origUrl;
    }
    vi.resetModules();
  });

  it("returns relative paths in the standard app build target", async () => {
    const mod = await loadLinks({});
    expect(mod.signinHref()).toBe("/login");
    expect(mod.signupHref()).toBe("/register");
    expect(mod.IS_APEX_BUILD).toBe(false);
  });

  it("returns absolute BRAND_APP_URL paths in the apex build target", async () => {
    const mod = await loadLinks({ target: "apex" });
    expect(mod.signinHref()).toBe("https://app.thebetterdecision.com/login");
    expect(mod.signupHref()).toBe("https://app.thebetterdecision.com/register");
    expect(mod.IS_APEX_BUILD).toBe(true);
    expect(mod.BRAND_APP_URL).toBe("https://app.thebetterdecision.com");
  });

  it("honours NEXT_PUBLIC_APP_URL override and strips trailing slash", async () => {
    const mod = await loadLinks({ target: "apex", appUrl: "https://staging.example.com/" });
    expect(mod.BRAND_APP_URL).toBe("https://staging.example.com");
    expect(mod.signupHref()).toBe("https://staging.example.com/register");
  });

  it("ignores empty NEXT_PUBLIC_APP_URL and falls back to the default", async () => {
    const mod = await loadLinks({ target: "apex", appUrl: "" });
    expect(mod.BRAND_APP_URL).toBe("https://app.thebetterdecision.com");
  });
});
