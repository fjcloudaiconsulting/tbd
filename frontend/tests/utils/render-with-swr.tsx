import type { ReactElement, ReactNode } from "react";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import { SWRConfig } from "swr";

/**
 * Renders `ui` inside a fresh-cache `<SWRConfig>` so each test gets an
 * isolated SWR cache. SWR's default cache is module-scoped, which would
 * let one test's resolved data leak into a later test that mounts the
 * same hook. A per-call `Map` isolates the cache, and `dedupingInterval: 0`
 * disables request coalescing so back-to-back renders in a single test each
 * issue their own fetch.
 *
 * The returned `rerender` is overridden to re-wrap in the SAME `<SWRConfig>`,
 * so call sites can do `rerender(<Foo … />)` without re-introducing an inline
 * wrapper. The cache Map is created once per call and reused across rerenders
 * (the provider is only read on mount; a fresh Map would silently reset SWR
 * state mid-test).
 *
 * This is the single shared replacement for the ~20 hand-rolled
 * `renderIsolated` / inline `<SWRConfig value={{ provider: () => new Map() }}>`
 * wrappers that used to be copy-pasted across the test suite.
 *
 * `options` are forwarded to Testing Library's `render` (e.g. `wrapper`,
 * `container`), so call sites that need extra render config keep working.
 */
export function renderWithSWR(
  ui: ReactElement,
  options?: RenderOptions,
): RenderResult {
  const cache = new Map();
  const withSWR = (node: ReactNode) => (
    <SWRConfig value={{ provider: () => cache, dedupingInterval: 0 }}>
      {node}
    </SWRConfig>
  );
  const result = render(withSWR(ui), options);
  return {
    ...result,
    rerender: (node: ReactNode) => result.rerender(withSWR(node)),
  };
}

export { screen, waitFor, fireEvent, act, within } from "@testing-library/react";
