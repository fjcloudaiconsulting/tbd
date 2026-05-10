import "@testing-library/jest-dom/vitest";

// Reset localStorage between tests so persisted sort/filter state from a
// previous test doesn't bleed into the next render. The persistence hooks
// (lib/hooks/use-persisted-sort, use-persisted-filters) read on mount, so
// without this isolation a test that exercises a non-default sort would
// alter the fixture for everything that follows.
beforeEach(() => {
  if (typeof window !== "undefined") {
    window.localStorage.clear();
  }
});
