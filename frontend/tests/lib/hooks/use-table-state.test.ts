import { act, renderHook } from "@testing-library/react";

import {
  PAGE_SIZE_OPTIONS,
  paginate,
  pageCount,
  useTableState,
} from "@/lib/hooks/use-table-state";
import { writePersisted } from "@/lib/persisted-state";

const KEY = "pfv:test:table-state";

// localStorage is cleared between each test by vitest.setup.ts

describe("paginate helper", () => {
  it("returns an empty array for an empty list", () => {
    expect(paginate([], 1, 25)).toEqual([]);
  });

  it("returns the first page slice", () => {
    const rows = [1, 2, 3, 4, 5];
    expect(paginate(rows, 1, 3)).toEqual([1, 2, 3]);
  });

  it("returns the second page slice", () => {
    const rows = [1, 2, 3, 4, 5];
    expect(paginate(rows, 2, 3)).toEqual([4, 5]);
  });

  it("returns an empty array when page is beyond the data", () => {
    const rows = [1, 2, 3];
    expect(paginate(rows, 2, 3)).toEqual([]);
  });

  it("handles exact multiples correctly", () => {
    const rows = [1, 2, 3, 4, 5, 6];
    expect(paginate(rows, 2, 3)).toEqual([4, 5, 6]);
    expect(paginate(rows, 3, 2)).toEqual([5, 6]);
  });
});

describe("pageCount helper", () => {
  it("returns 1 for an empty list", () => {
    expect(pageCount(0, 25)).toBe(1);
  });

  it("returns 1 when total <= pageSize", () => {
    expect(pageCount(10, 25)).toBe(1);
    expect(pageCount(25, 25)).toBe(1);
  });

  it("returns the correct page count for an exact multiple", () => {
    expect(pageCount(50, 25)).toBe(2);
    expect(pageCount(100, 25)).toBe(4);
  });

  it("rounds up for a partial last page", () => {
    expect(pageCount(26, 25)).toBe(2);
    expect(pageCount(51, 25)).toBe(3);
  });
});

describe("PAGE_SIZE_OPTIONS", () => {
  it("exports [10, 25, 50, 100]", () => {
    expect(PAGE_SIZE_OPTIONS).toEqual([10, 25, 50, 100]);
  });
});

describe("useTableState", () => {
  it("returns the supplied defaults when localStorage is empty", () => {
    const { result } = renderHook(() =>
      useTableState<"date" | "amount">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "desc",
      }),
    );
    expect(result.current.sortField).toBe("date");
    expect(result.current.sortDir).toBe("desc");
    expect(result.current.page).toBe(1);
    expect(result.current.pageSize).toBe(25);
  });

  it("respects defaultPageSize option", () => {
    const { result } = renderHook(() =>
      useTableState<"date">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "asc",
        defaultPageSize: 50,
      }),
    );
    expect(result.current.pageSize).toBe(50);
  });

  it("rehydrates persisted sortField, sortDir, and pageSize on mount", () => {
    writePersisted(KEY, { sortField: "amount", sortDir: "asc", pageSize: 50 });
    const { result } = renderHook(() =>
      useTableState<"date" | "amount">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "desc",
        allowedSortFields: ["date", "amount"],
      }),
    );
    expect(result.current.sortField).toBe("amount");
    expect(result.current.sortDir).toBe("asc");
    expect(result.current.pageSize).toBe(50);
    // page is never persisted — always starts at 1
    expect(result.current.page).toBe(1);
  });

  it("falls back to defaults when stored sortField is not in allowedSortFields", () => {
    writePersisted(KEY, {
      sortField: "removed_column",
      sortDir: "asc",
      pageSize: 25,
    });
    const { result } = renderHook(() =>
      useTableState<"date" | "amount">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "desc",
        allowedSortFields: ["date", "amount"],
      }),
    );
    expect(result.current.sortField).toBe("date");
    expect(result.current.sortDir).toBe("desc");
  });

  it("falls back to defaults when persisted data is malformed", () => {
    window.localStorage.setItem(KEY, "{bad json");
    const { result } = renderHook(() =>
      useTableState<"date">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "asc",
      }),
    );
    expect(result.current.sortField).toBe("date");
    expect(result.current.sortDir).toBe("asc");
    expect(result.current.pageSize).toBe(25);
  });

  it("setSort updates sortField, sortDir, and resets page to 1", () => {
    const { result } = renderHook(() =>
      useTableState<"date" | "amount">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "desc",
      }),
    );

    // First advance to page 3
    act(() => {
      result.current.setPage(3);
    });
    expect(result.current.page).toBe(3);

    // Changing sort must reset page
    act(() => {
      result.current.setSort("amount", "asc");
    });
    expect(result.current.sortField).toBe("amount");
    expect(result.current.sortDir).toBe("asc");
    expect(result.current.page).toBe(1);
  });

  it("setSort writes sortField, sortDir, pageSize to localStorage", () => {
    const { result } = renderHook(() =>
      useTableState<"date" | "amount">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "desc",
      }),
    );
    act(() => {
      result.current.setSort("amount", "asc");
    });
    const stored = JSON.parse(window.localStorage.getItem(KEY)!);
    expect(stored.sortField).toBe("amount");
    expect(stored.sortDir).toBe("asc");
    // pageSize should also be persisted
    expect(typeof stored.pageSize).toBe("number");
  });

  it("setPage updates page without resetting sort", () => {
    const { result } = renderHook(() =>
      useTableState<"date">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "asc",
      }),
    );
    act(() => {
      result.current.setPage(4);
    });
    expect(result.current.page).toBe(4);
    expect(result.current.sortField).toBe("date");
  });

  it("setPageSize updates pageSize and resets page to 1", () => {
    const { result } = renderHook(() =>
      useTableState<"date">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "asc",
      }),
    );

    // Advance to page 3 first
    act(() => {
      result.current.setPage(3);
    });
    expect(result.current.page).toBe(3);

    act(() => {
      result.current.setPageSize(50);
    });
    expect(result.current.pageSize).toBe(50);
    expect(result.current.page).toBe(1);
  });

  it("setPageSize writes to localStorage", () => {
    const { result } = renderHook(() =>
      useTableState<"date">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "asc",
      }),
    );
    act(() => {
      result.current.setPageSize(100);
    });
    const stored = JSON.parse(window.localStorage.getItem(KEY)!);
    expect(stored.pageSize).toBe(100);
  });

  it("reset() restores all defaults and clears localStorage", () => {
    writePersisted(KEY, { sortField: "amount", sortDir: "asc", pageSize: 50 });
    const { result } = renderHook(() =>
      useTableState<"date" | "amount">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "desc",
        allowedSortFields: ["date", "amount"],
      }),
    );

    // Sanity-check it rehydrated
    expect(result.current.sortField).toBe("amount");

    // Advance to some page
    act(() => {
      result.current.setPage(5);
    });

    act(() => {
      result.current.reset();
    });

    expect(result.current.sortField).toBe("date");
    expect(result.current.sortDir).toBe("desc");
    expect(result.current.page).toBe(1);
    expect(result.current.pageSize).toBe(25);
    // After reset(), clearPersisted removes the key, but the persist effect
    // immediately re-writes the defaults on the next render. Assert that the
    // stored value reflects the defaults rather than asserting absence.
    const afterReset = JSON.parse(window.localStorage.getItem(KEY)!);
    expect(afterReset.sortField).toBe("date");
    expect(afterReset.sortDir).toBe("desc");
    expect(afterReset.pageSize).toBe(25);
  });

  it("page is never persisted — a fresh hook always starts at page 1", () => {
    const { result: r1 } = renderHook(() =>
      useTableState<"date">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "asc",
      }),
    );
    act(() => {
      r1.current.setPage(7);
    });

    // Re-mount to simulate navigation away and back
    const { result: r2 } = renderHook(() =>
      useTableState<"date">({
        key: KEY,
        defaultSortField: "date",
        defaultSortDir: "asc",
      }),
    );
    expect(r2.current.page).toBe(1);
  });
});
