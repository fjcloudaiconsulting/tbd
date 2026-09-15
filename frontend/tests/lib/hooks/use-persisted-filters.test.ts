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
    // `account` is missing; should keep the default.
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

  it("rejects objects, and arrays for fields whose default is not an array", () => {
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

  describe("array fields (TBD-464)", () => {
    type MultiFilters = { accounts: number[]; tags: string[]; search: string };
    const MULTI_DEFAULTS: MultiFilters = { accounts: [], tags: [], search: "" };

    it("keeps a stored array of primitives", () => {
      // FENCE. Kills: arrays silently dropped by the primitive-only merge.
      window.localStorage.setItem(
        KEY,
        JSON.stringify({ accounts: [1, 2], tags: ["a", "b"] }),
      );
      const { result } = renderHook(() =>
        usePersistedFilters<MultiFilters>(KEY, MULTI_DEFAULTS),
      );
      expect(result.current.filters.accounts).toEqual([1, 2]);
      expect(result.current.filters.tags).toEqual(["a", "b"]);
    });

    it("migrates a scalar stored before the field became an array", () => {
      // FENCE. Kills: an old `{filterAccount: 5}` payload dropped or crashing.
      window.localStorage.setItem(
        KEY,
        JSON.stringify({ accounts: 5, tags: "" }),
      );
      const { result } = renderHook(() =>
        usePersistedFilters<MultiFilters>(KEY, MULTI_DEFAULTS),
      );
      expect(result.current.filters.accounts).toEqual([5]);
      expect(result.current.filters.tags).toEqual([]);
    });

    it("migrates null to an empty array and rejects nested values", () => {
      window.localStorage.setItem(
        KEY,
        JSON.stringify({ accounts: null, tags: [{ x: 1 }] }),
      );
      const { result } = renderHook(() =>
        usePersistedFilters<MultiFilters>(KEY, MULTI_DEFAULTS),
      );
      expect(result.current.filters.accounts).toEqual([]);
      expect(result.current.filters.tags).toEqual([]);
    });

    it("compares arrays element-wise for isDefault", () => {
      // FENCE. Kills: `[] !== []`, which leaves isDefault false forever once a
      // fresh empty array (a cleared select, a migrated "") is in state.
      window.localStorage.setItem(
        KEY,
        JSON.stringify({ accounts: "", tags: "", search: "" }),
      );
      const { result } = renderHook(() =>
        usePersistedFilters<MultiFilters>(KEY, MULTI_DEFAULTS),
      );
      expect(result.current.isDefault).toBe(true);
      act(() => result.current.set({ accounts: [3] }));
      expect(result.current.isDefault).toBe(false);
      act(() => result.current.set({ accounts: [] }));
      expect(result.current.isDefault).toBe(true);
    });
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
