import type { ReactElement, ReactNode } from "react";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import { SWRConfig } from "swr";

/**
 * Renders `ui` inside a fresh-cache `<SWRConfig>` so each test gets an
 * isolated SWR cache. SWR's default cache is module-scoped, which would
 * let one test's resolved data leak into a later test that mounts the
 * same hook. The `provider: () => new Map()` factory hands every mount a
 * brand-new Map, and `dedupingInterval: 0` disables request coalescing so
 * back-to-back renders in a single test each issue their own fetch.
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
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui as ReactNode}
    </SWRConfig>,
    options,
  );
}

export { screen, waitFor, fireEvent, act, within } from "@testing-library/react";
