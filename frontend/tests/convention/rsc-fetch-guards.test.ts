import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import path from "node:path";
import { describe, it, expect } from "vitest";

// Release gate against the #282 class of bug.
//
// Surfaces that run on the server during render OR are server-only:
//   - All RSC pages (app/**/page.tsx) and layouts (app/**/layout.tsx)
//   - server-only library code (lib/*-server.ts and lib/server-*.ts)
//   - The instrumentation entry point (instrumentation.ts)
//
// In these files a thrown error or rejected promise from `fetch(...)` is
// not caught by any client-side mechanism and surfaces as the Next.js
// error boundary with an opaque digest reference. The sanctioned helper
// `frontend/lib/server-fetch.ts` owns the try/catch + sanitized logging.
//
// This test scans the server surface and rejects any direct `fetch(` call
// outside a small allowlist, so the unguarded-RSC-fetch class that
// produced "Reference: 1621627876" cannot land again.

const FRONTEND_ROOT = path.resolve(__dirname, "..", "..");

type Walker = { include: (file: string) => boolean; roots: string[] };

const WALKERS: Walker[] = [
  // app/**/page.tsx and app/**/layout.tsx
  {
    roots: ["app"],
    include: (f) => f.endsWith("/page.tsx") || f.endsWith("/layout.tsx"),
  },
  // lib/*-server.ts and lib/server-*.ts (NON-recursive: top of lib/ only)
  {
    roots: ["lib"],
    include: (f) => {
      const base = path.basename(f);
      // Only the top level of lib/ — depth check below.
      return (
        (base.endsWith("-server.ts") || base.startsWith("server-")) &&
        base.endsWith(".ts")
      );
    },
  },
];

// Files allowed to call fetch directly with a documented reason. Keep
// SMALL. Paths are relative to frontend/.
const ALLOWLIST: { path: string; reason: string }[] = [
  {
    path: "lib/server-fetch.ts",
    reason: "the helper itself owns the sanctioned fetch call",
  },
];

function walk(dir: string, depth: number, maxDepth: number): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (depth < maxDepth) {
        out.push(...walk(full, depth + 1, maxDepth));
      }
    } else if (st.isFile()) {
      out.push(full);
    }
  }
  return out;
}

function collectServerSurface(): string[] {
  const files = new Set<string>();

  // app/**/page.tsx, app/**/layout.tsx — recursive, no depth bound that
  // would matter for this project.
  const appRoot = path.join(FRONTEND_ROOT, "app");
  for (const f of walk(appRoot, 0, 20)) {
    if (f.endsWith("/page.tsx") || f.endsWith("/layout.tsx")) {
      files.add(f);
    }
  }

  // lib/server-*.ts and lib/*-server.ts at the top of lib/ (NON-recursive).
  const libRoot = path.join(FRONTEND_ROOT, "lib");
  if (existsSync(libRoot)) {
    for (const entry of readdirSync(libRoot)) {
      const full = path.join(libRoot, entry);
      const st = statSync(full);
      if (!st.isFile()) continue;
      if (!entry.endsWith(".ts")) continue;
      if (entry.endsWith("-server.ts") || entry.startsWith("server-")) {
        files.add(full);
      }
    }
  }

  // instrumentation.ts at the frontend root.
  const inst = path.join(FRONTEND_ROOT, "instrumentation.ts");
  if (existsSync(inst)) files.add(inst);

  return Array.from(files);
}

function isAllowlisted(absPath: string): boolean {
  const rel = path.relative(FRONTEND_ROOT, absPath).split(path.sep).join("/");
  return ALLOWLIST.some((a) => rel === a.path);
}

// Find every `fetch(` call site in the source by line number. We strip
// block and line comments first so a `fetch(` inside a comment doesn't
// trigger the gate, then walk line-by-line so the reported line number
// points at the original source.
function findFetchCallLines(src: string): number[] {
  // Strip block comments. Preserve newlines so line numbers stay aligned.
  const noBlock = src.replace(/\/\*[\s\S]*?\*\//g, (m) =>
    m.replace(/[^\n]/g, " "),
  );
  // Strip line comments. Preserve the line itself (the // and after are
  // blanked, but the newline survives).
  const noComments = noBlock.replace(/\/\/[^\n]*/g, (m) =>
    m.replace(/[^\n]/g, " "),
  );

  const lines = noComments.split("\n");
  const hits: number[] = [];
  const re = /\bfetch\s*\(/;
  for (let i = 0; i < lines.length; i++) {
    if (re.test(lines[i])) hits.push(i + 1);
  }
  return hits;
}

describe("RSC/server fetch guard", () => {
  it("rejects direct backend fetch() in server surfaces", () => {
    void WALKERS; // documentation; actual collection is inlined above
    const files = collectServerSurface().filter((f) => !isAllowlisted(f));

    const violations: { file: string; lines: number[] }[] = [];
    for (const file of files) {
      const src = readFileSync(file, "utf-8");
      const hits = findFetchCallLines(src);
      if (hits.length > 0) {
        const rel = path
          .relative(FRONTEND_ROOT, file)
          .split(path.sep)
          .join("/");
        violations.push({ file: rel, lines: hits });
      }
    }

    if (violations.length > 0) {
      const detail = violations
        .map((v) => `  ${v.file}: lines ${v.lines.join(", ")}`)
        .join("\n");
      throw new Error(
        "Direct fetch() in server surface. Use serverFetch from " +
          "frontend/lib/server-fetch.ts.\n" +
          detail,
      );
    }

    expect(violations).toEqual([]);
  });
});
