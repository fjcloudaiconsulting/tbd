import { act, renderHook } from "@testing-library/react";

import { usePersistedFilters } from "@/lib/hooks/use-persisted-filters";

const KEY = "pfv:test:filters";

type Filters = {
  search: string;
  account: number | "";
  status: string;
};

const DEFAULTS: Filters = {
  search: "",
  account: "",
  status: "",
};

beforeEach(() => {
  window.localStorage.clear();
});

describe("usePersistedFilters", () => {
  it("applies the supplied defaults when localStorage is empty", () => {
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    expect(result.current.filters).toEqual(DEFAULTS);
    expect(result.current.isDefault).toBe(true);
  });

  it("rehydrates persisted values on mount", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ search: "rent", account: 42, status: "settled" }),
    );
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    expect(result.current.filters).toEqual({
      search: "rent",
      account: 42,
      status: "settled",
    });
    expect(result.current.isDefault).toBe(false);
  });

  it("set() merges and writes through to localStorage", () => {
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    act(() => result.current.set({ search: "rent" }));
    expect(result.current.filters.search).toBe("rent");
    expect(result.current.filters.account).toBe("");
    const raw = window.localStorage.getItem(KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).search).toBe("rent");
  });

  it("setField() writes a single field through", () => {
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    act(() => result.current.setField("status", "pending"));
    expect(result.current.filters.status).toBe("pending");
    expect(JSON.parse(window.localStorage.getItem(KEY)!).status).toBe(
      "pending",
    );
  });

  it("reset() clears persistence and returns to defaults", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ search: "x", account: 1, status: "settled" }),
    );
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    expect(result.current.isDefault).toBe(false);

    act(() => result.current.reset());
    expect(result.current.filters).toEqual(DEFAULTS);
    expect(result.current.isDefault).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });

  it("falls through to defaults on malformed JSON", () => {
    window.localStorage.setItem(KEY, "<<<corrupt>>>");
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    expect(result.current.filters).toEqual(DEFAULTS);
  });

  it("merges over defaults so a stale payload missing a field still works", () => {
    // `account` is missing — should keep the default.
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ search: "groceries", status: "settled" }),
    );
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    expect(result.current.filters.account).toBe("");
    expect(result.current.filters.search).toBe("groceries");
    expect(result.current.filters.status).toBe("settled");
  });

  it("rejects non-primitive stored values for known fields", () => {
    // Fields with object/array stored values fall back to defaults.
    // Primitives (string/number/boolean/null) are accepted because union
    // types like `number | ""` are common in this codebase.
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        search: { nope: true },
        account: 1,
        status: ["bogus"],
      }),
    );
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    expect(result.current.filters.search).toBe("");
    expect(result.current.filters.account).toBe(1);
    expect(result.current.filters.status).toBe("");
  });

  it("isDefault flips with set/reset", () => {
    const { result } = renderHook(() =>
      usePersistedFilters<Filters>(KEY, DEFAULTS),
    );
    expect(result.current.isDefault).toBe(true);
    act(() => result.current.set({ search: "x" }));
    expect(result.current.isDefault).toBe(false);
    act(() => result.current.set({ search: "" }));
    expect(result.current.isDefault).toBe(true);
  });
});
