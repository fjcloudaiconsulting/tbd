"use client";

// usePersistedSort — sort field + direction, persisted to localStorage so a
// user's chosen order survives navigation and reloads. Punch list item 6
// (system-wide sort persistence) and item 16 (Dashboard Spending card)
// consume this hook.
//
// Hydration is one-shot on mount via lazy useState init, so the first render
// already shows the persisted state on the client. (SSR returns the default;
// hooks like this only mount on client pages.) Every setSort call writes
// through, and reset() restores the constructor defaults and removes the
// localStorage entry so a future visitor sees a clean slate.

import { useCallback, useState } from "react";

import {
  clearPersisted,
  readPersisted,
  writePersisted,
} from "@/lib/persisted-state";

export type SortDir = "asc" | "desc";

export interface PersistedSort<F extends string> {
  field: F;
  dir: SortDir;
  setSort: (field: F, dir: SortDir) => void;
  reset: () => void;
  isDefault: boolean;
}

interface StoredSort {
  field: string;
  dir: SortDir;
}

function isStoredSort(value: unknown): value is StoredSort {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.field === "string" && (v.dir === "asc" || v.dir === "desc")
  );
}

export function usePersistedSort<F extends string>(
  key: string,
  defaultField: F,
  defaultDir: SortDir,
  allowedFields?: readonly F[],
): PersistedSort<F> {
  const [state, setState] = useState<StoredSort>(() => {
    const stored = readPersisted<StoredSort>(
      key,
      { field: defaultField, dir: defaultDir },
      isStoredSort,
    );
    // If the caller passed a whitelist, drop unknown fields. This handles
    // schema drift (a column was removed) without throwing.
    if (allowedFields && !allowedFields.includes(stored.field as F)) {
      return { field: defaultField, dir: defaultDir };
    }
    return stored;
  });

  const setSort = useCallback(
    (field: F, dir: SortDir) => {
      const next = { field, dir };
      setState(next);
      writePersisted(key, next);
    },
    [key],
  );

  const reset = useCallback(() => {
    setState({ field: defaultField, dir: defaultDir });
    clearPersisted(key);
  }, [key, defaultField, defaultDir]);

  const isDefault = state.field === defaultField && state.dir === defaultDir;

  return {
    field: state.field as F,
    dir: state.dir,
    setSort,
    reset,
    isDefault,
  };
}
