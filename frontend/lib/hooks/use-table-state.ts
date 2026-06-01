"use client";

// useTableState: composed sort + pagination state persisted to localStorage.
// Sort field, sort direction, and page size survive navigation and reloads.
// Page number is intentionally not persisted — it always resets to 1 on mount
// so users don't land on a stale deep page after coming back to a list view.
//
// Changing sort or page size resets the page to 1 automatically — you don't
// want to be stuck on page 5 after re-sorting a different column.

import { useCallback, useState } from "react";

import {
  clearPersisted,
  readPersisted,
  writePersisted,
} from "@/lib/persisted-state";
import type { SortDir } from "@/lib/hooks/use-persisted-sort";

export type { SortDir };

export const PAGE_SIZE_OPTIONS = [10, 25, 50, 100] as const;

// ---------------------------------------------------------------------------
// Pure helpers (exported so callers can use them without the hook)
// ---------------------------------------------------------------------------

/** Returns the slice of `rows` for the given 1-based `page` and `pageSize`. */
export function paginate<T>(rows: T[], page: number, pageSize: number): T[] {
  const start = (page - 1) * pageSize;
  return rows.slice(start, start + pageSize);
}

/**
 * Returns the number of pages needed to display `total` rows at `pageSize`
 * items per page. Always returns at least 1.
 */
export function pageCount(total: number, pageSize: number): number {
  if (total === 0) return 1;
  return Math.ceil(total / pageSize);
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export interface TableStateOptions<F extends string> {
  /** localStorage key under which sort + pageSize are persisted. */
  key: string;
  defaultSortField: F;
  defaultSortDir: SortDir;
  /**
   * If supplied, a stored sortField that is not in this list is discarded and
   * the hook falls back to the default. Handles schema drift gracefully.
   */
  allowedSortFields?: readonly F[];
  /** Defaults to 25. */
  defaultPageSize?: number;
}

export interface TableState<F extends string> {
  sortField: F;
  sortDir: SortDir;
  setSort: (field: F, dir: SortDir) => void;
  page: number;
  setPage: (n: number) => void;
  pageSize: number;
  setPageSize: (n: number) => void;
  reset: () => void;
}

interface StoredState {
  sortField: string;
  sortDir: SortDir;
  pageSize: number;
}

function isStoredState(value: unknown): value is StoredState {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.sortField === "string" &&
    (v.sortDir === "asc" || v.sortDir === "desc") &&
    typeof v.pageSize === "number"
  );
}

export function useTableState<F extends string>(
  opts: TableStateOptions<F>,
): TableState<F> {
  const {
    key,
    defaultSortField,
    defaultSortDir,
    allowedSortFields,
    defaultPageSize = 25,
  } = opts;

  const [sortField, setSortFieldState] = useState<F>(() => {
    const stored = readPersisted<StoredState>(
      key,
      {
        sortField: defaultSortField,
        sortDir: defaultSortDir,
        pageSize: defaultPageSize,
      },
      isStoredState,
    );
    if (
      allowedSortFields &&
      !allowedSortFields.includes(stored.sortField as F)
    ) {
      return defaultSortField;
    }
    return stored.sortField as F;
  });

  const [sortDir, setSortDirState] = useState<SortDir>(() => {
    const stored = readPersisted<StoredState>(
      key,
      {
        sortField: defaultSortField,
        sortDir: defaultSortDir,
        pageSize: defaultPageSize,
      },
      isStoredState,
    );
    // If sortField was invalid, also reset dir to default
    if (
      allowedSortFields &&
      !allowedSortFields.includes(stored.sortField as F)
    ) {
      return defaultSortDir;
    }
    return stored.sortDir;
  });

  const [pageSize, setPageSizeState] = useState<number>(() => {
    const stored = readPersisted<StoredState>(
      key,
      {
        sortField: defaultSortField,
        sortDir: defaultSortDir,
        pageSize: defaultPageSize,
      },
      isStoredState,
    );
    return stored.pageSize;
  });

  // page is never persisted — always starts at 1
  const [page, setPageState] = useState<number>(1);

  const persist = useCallback(
    (field: F, dir: SortDir, size: number) => {
      writePersisted<StoredState>(key, {
        sortField: field,
        sortDir: dir,
        pageSize: size,
      });
    },
    [key],
  );

  const setSort = useCallback(
    (field: F, dir: SortDir) => {
      setSortFieldState(field);
      setSortDirState(dir);
      setPageState(1);
      // Read current pageSize from state lazily — pass it as a closure via
      // functional state update pattern to keep the persist call accurate.
      setPageSizeState((currentSize) => {
        persist(field, dir, currentSize);
        return currentSize;
      });
    },
    [persist],
  );

  const setPage = useCallback((n: number) => {
    setPageState(n);
  }, []);

  const setPageSize = useCallback(
    (n: number) => {
      setPageSizeState(n);
      setPageState(1);
      // Access current sort state via closure — values captured are the
      // latest render values since setPageSize is recreated on each render
      // when sortField/sortDir change... but since we use useCallback with
      // stable deps, we need to read from state. Use functional updaters:
      setSortFieldState((currentField) => {
        setSortDirState((currentDir) => {
          persist(currentField, currentDir, n);
          return currentDir;
        });
        return currentField;
      });
    },
    [persist],
  );

  const reset = useCallback(() => {
    setSortFieldState(defaultSortField);
    setSortDirState(defaultSortDir);
    setPageSizeState(defaultPageSize);
    setPageState(1);
    clearPersisted(key);
  }, [key, defaultSortField, defaultSortDir, defaultPageSize]);

  return {
    sortField,
    sortDir,
    setSort,
    page,
    setPage,
    pageSize,
    setPageSize,
    reset,
  };
}
