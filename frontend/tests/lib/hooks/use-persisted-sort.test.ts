import { act, renderHook } from "@testing-library/react";

import { usePersistedSort } from "@/lib/hooks/use-persisted-sort";

const KEY = "pfv:test:sort";

beforeEach(() => {
  window.localStorage.clear();
});

describe("usePersistedSort", () => {
  it("applies the supplied defaults when localStorage is empty", () => {
    const { result } = renderHook(() =>
      usePersistedSort<"date" | "amount">(KEY, "date", "desc"),
    );
    expect(result.current.field).toBe("date");
    expect(result.current.dir).toBe("desc");
    expect(result.current.isDefault).toBe(true);
  });

  it("rehydrates persisted values on mount", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ field: "amount", dir: "asc" }),
    );
    const { result } = renderHook(() =>
      usePersistedSort<"date" | "amount">(KEY, "date", "desc"),
    );
    expect(result.current.field).toBe("amount");
    expect(result.current.dir).toBe("asc");
    expect(result.current.isDefault).toBe(false);
  });

  it("setSort writes through to localStorage", () => {
    const { result } = renderHook(() =>
      usePersistedSort<"date" | "amount">(KEY, "date", "desc"),
    );
    act(() => {
      result.current.setSort("amount", "asc");
    });
    expect(result.current.field).toBe("amount");
    expect(result.current.dir).toBe("asc");
    expect(window.localStorage.getItem(KEY)).toBe(
      JSON.stringify({ field: "amount", dir: "asc" }),
    );
  });

  it("reset() clears persistence and returns to defaults", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ field: "amount", dir: "asc" }),
    );
    const { result } = renderHook(() =>
      usePersistedSort<"date" | "amount">(KEY, "date", "desc"),
    );
    expect(result.current.field).toBe("amount");

    act(() => {
      result.current.reset();
    });
    expect(result.current.field).toBe("date");
    expect(result.current.dir).toBe("desc");
    expect(result.current.isDefault).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });

  it("falls through to defaults on malformed JSON without throwing", () => {
    window.localStorage.setItem(KEY, "{not json");
    const { result } = renderHook(() =>
      usePersistedSort<"date" | "amount">(KEY, "date", "desc"),
    );
    expect(result.current.field).toBe("date");
    expect(result.current.dir).toBe("desc");
  });

  it("falls through to defaults when stored shape is invalid", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ field: 42, dir: "sideways" }),
    );
    const { result } = renderHook(() =>
      usePersistedSort<"date" | "amount">(KEY, "date", "desc"),
    );
    expect(result.current.field).toBe("date");
    expect(result.current.dir).toBe("desc");
  });

  it("falls through to defaults when stored field is no longer allowed", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ field: "removed_column", dir: "asc" }),
    );
    const { result } = renderHook(() =>
      usePersistedSort<"date" | "amount">(KEY, "date", "desc", [
        "date",
        "amount",
      ]),
    );
    expect(result.current.field).toBe("date");
    expect(result.current.dir).toBe("desc");
  });

  it("isDefault flips to false after a non-default setSort and back to true on reset", () => {
    const { result } = renderHook(() =>
      usePersistedSort<"date" | "amount">(KEY, "date", "desc"),
    );
    expect(result.current.isDefault).toBe(true);
    act(() => result.current.setSort("amount", "asc"));
    expect(result.current.isDefault).toBe(false);
    act(() => result.current.reset());
    expect(result.current.isDefault).toBe(true);
  });
});
