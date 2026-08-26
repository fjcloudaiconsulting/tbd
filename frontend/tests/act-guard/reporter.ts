/**
 * Emits the per-(file, component) act() warning tally to a JSON artifact.
 * TBD-393.
 *
 * ⚠ THIS REPORTER NEVER COMPARES AND NEVER FAILS. Emit and judge are kept
 * separate so the judge (`scripts/act-baseline.mjs`) is a pure function over
 * two JSON files, runnable and testable without booting vitest.
 *
 * ⚠ `partial` MARKS A FILTERED RUN. `npm test -- tests/foo.test.tsx` is the
 * documented dev workflow, and comparing one file's tally against the full
 * baseline would fail every time. The judge skips (loudly) on a partial run
 * locally, and REFUSES under CI so nobody can neuter the gate by adding a
 * filter to the workflow.
 *
 * ⚠ THE ARTIFACT IS UNLINKED AT onInit. A stale file from a previous run
 * must never be judged as if it were this run's.
 */

import { rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const ARTIFACT = resolve(process.cwd(), ".act-warnings.json");

type Tally = Record<string, Record<string, number>>;

function isFiltered(argv: string[]): boolean {
  return argv.some(
    (a) =>
      a === "--shard" ||
      a.startsWith("--shard=") ||
      a === "-t" ||
      a === "--testNamePattern" ||
      a.startsWith("--testNamePattern=") ||
      // a bare positional that is not a flag and not the `run` subcommand
      (!a.startsWith("-") && a !== "run"),
  );
}

export default class ActWarningReporter {
  private argv: string[] = [];
  private shard: unknown = null;

  onInit(ctx?: { config?: { shard?: unknown } }) {
    this.argv = process.argv.slice(2);
    this.shard = ctx?.config?.shard ?? null;
    try {
      rmSync(ARTIFACT);
    } catch {
      /* absent is fine — this is the normal case */
    }
  }

  private emit(modules: Iterable<unknown>) {
    const files: Tally = {};
    for (const mod of modules) {
      const m = mod as {
        moduleId?: string;
        filepath?: string;
        meta?: (() => Record<string, unknown>) | Record<string, unknown>;
        task?: { meta?: Record<string, unknown> };
      };
      const raw = typeof m.meta === "function" ? m.meta() : (m.meta ?? m.task?.meta);
      const counts = (raw as { actWarnings?: Record<string, number> })?.actWarnings;
      if (!counts) continue;
      const id = m.moduleId ?? m.filepath ?? "";
      const rel = id.replace(process.cwd() + "/", "").split("\\").join("/");
      files[rel] = { ...(files[rel] ?? {}), ...counts };
    }
    writeFileSync(
      ARTIFACT,
      JSON.stringify(
        {
          schemaVersion: 1,
          partial: isFiltered(this.argv),
          shard: this.shard,
          argv: this.argv,
          files: Object.fromEntries(Object.entries(files).sort()),
        },
        null,
        2,
      ) + "\n",
    );
  }

  // Vitest 3 name.
  onTestRunEnd(testModules: Iterable<unknown>) {
    this.emit(testModules ?? []);
  }

  // Legacy name, kept so a version skew degrades to a WRITE rather than to
  // silence. If neither fires, the artifact is absent and the judge fails
  // hard — which is the correct direction.
  onFinished(files: Iterable<unknown>) {
    this.emit(files ?? []);
  }
}
