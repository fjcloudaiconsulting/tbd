"use client";

// useTableState: composed sort + pagination state persisted to localStorage.
// Sort field, sort direction, and page size survive navigation and reloads.
// Page number is intentionally not persisted — it always resets to 1 on mount
// so users don't land on a stale deep page after coming back to a list view.
//
// Changing sort or page size resets the page to 1 automatically — you don't
// want to be stuck on page 5 after re-sorting a different column.

import { useCallback, useEffect, useState } from "react";

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
 *
 * Guards against corrupted pageSize values (0, NaN, Infinity, negative) by
 * treating them as a single-page result rather than producing Infinity/NaN.
 */
export function pageCount(total: number, pageSize: number): number {
  if (!Number.isFinite(pageSize) || pageSize <= 0) return 1;
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
    Number.isInteger(v.pageSize) &&
    (v.pageSize as number) > 0
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

  // All three persisted fields live in a single state object so they can be
  // updated atomically and the persist effect has a stable dependency list.
  const [stored, setStored] = useState<StoredState>(() => {
    const persisted = readPersisted<StoredState>(
      key,
      {
        sortField: defaultSortField,
        sortDir: defaultSortDir,
        pageSize: defaultPageSize,
      },
      isStoredState,
    );
    // Discard a stored sortField that is no longer in the allowed set.
    if (
      allowedSortFields &&
      !allowedSortFields.includes(persisted.sortField as F)
    ) {
      return {
        sortField: defaultSortField,
        sortDir: defaultSortDir,
        pageSize: persisted.pageSize,
      };
    }
    return persisted;
  });

  // page is never persisted — always starts at 1
  const [page, setPageState] = useState<number>(1);

  // Persist sortField, sortDir, and pageSize whenever any of them change.
  // Using an effect keeps updater functions pure (no side effects inside them).
  // Persisting the defaults on mount is harmless and means localStorage always
  // reflects the current state after the first render.
  useEffect(() => {
    writePersisted<StoredState>(key, stored);
  }, [key, stored]);

  const setSort = useCallback(
    (field: F, dir: SortDir) => {
      setStored((prev) => ({ ...prev, sortField: field, sortDir: dir }));
      setPageState(1);
    },
    [],
  );

  const setPage = useCallback((n: number) => {
    setPageState(n);
  }, []);

  const setPageSize = useCallback((n: number) => {
    setStored((prev) => ({ ...prev, pageSize: n }));
    setPageState(1);
  }, []);

  const reset = useCallback(() => {
    setStored({
      sortField: defaultSortField,
      sortDir: defaultSortDir,
      pageSize: defaultPageSize,
    });
    setPageState(1);
    clearPersisted(key);
  }, [key, defaultSortField, defaultSortDir, defaultPageSize]);

  return {
    sortField: stored.sortField as F,
    sortDir: stored.sortDir,
    setSort,
    page,
    setPage,
    pageSize: stored.pageSize,
    setPageSize,
    reset,
  };
}
