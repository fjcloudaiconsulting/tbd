/**
 * Installs the console patch that counts React act() warnings per
 * (test file, component). TBD-393.
 *
 * IMPORTED ONLY FROM `vitest.setup.ts`. Nothing else may import this module.
 *
 * The canary self-test deliberately does NOT import it -- it reads the tally
 * through `globalThis.__actGuardCounts`, which exists only if setup loaded
 * this file. That is what lets the canary detect the import being dropped
 * from setup. If a test imported this module directly, that import would
 * install the patch itself and the canary would stay green in exactly the
 * scenario it exists to catch. The pure matcher lives in `classify.ts` so it
 * can be unit-tested without pulling this side effect in.
 *
 * NEVER SWALLOWS. The original console.error is always called through: the
 * warnings must stay visible to a human reading the log and to Vitest's own
 * console interception. This module observes; it does not silence.
 */

import { classify } from "./classify";

const counts = new Map<string, number>();

declare global {
  // eslint-disable-next-line no-var
  var __actGuardCounts: (() => Map<string, number>) | undefined;
}

globalThis.__actGuardCounts = () => counts;

const original = console.error;
console.error = function patchedConsoleError(this: unknown, ...args: unknown[]) {
  const name = classify(args);
  if (name !== null) counts.set(name, (counts.get(name) ?? 0) + 1);
  return original.apply(this, args as never);
} as typeof console.error;

// Hand this file's tally to the main process. `task.meta` is serialised back
// by Vitest; the reporter keys it by module path. Module state is per test
// file under the default `isolate: true`, which is exactly the scope wanted.
afterAll((suite) => {
  if (counts.size === 0) return;
  // Vitest types `meta` as its own TaskMeta interface; widening it here rather
  // than annotating the parameter, because annotating it does not satisfy
  // `AfterAllListener` and fails the production type-check while leaving the
  // test run green.
  const meta = (suite as unknown as { meta: Record<string, unknown> }).meta;
  meta.actWarnings = Object.fromEntries(counts);
});
