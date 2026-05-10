"use client";

// usePersistedFilters: per-surface filter object persisted to localStorage.
// Designed for the Transactions page (account/category/type/status/date
// range/search/period) but generic over any plain JSON-serializable filter
// shape. Punch list item 6 covers filter persistence alongside sort.
//
// The hook returns the current filters, a `set` function (partial merge), an
// individual-field `setField`, and `reset`. `set` writes through to
// localStorage on each call. `reset` clears the persisted entry. `isDefault`
// is a shallow comparison against the supplied defaults so consumers can show
// a "Reset filters and sort" affordance only when something is non-default.

import { useCallback, useState } from "react";

import {
  clearPersisted,
  readPersisted,
  writePersisted,
} from "@/lib/persisted-state";

export interface PersistedFilters<T extends Record<string, unknown>> {
  filters: T;
  set: (patch: Partial<T>) => void;
  setField: <K extends keyof T>(field: K, value: T[K]) => void;
  reset: () => void;
  isDefault: boolean;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function shallowEqual<T extends Record<string, unknown>>(a: T, b: T): boolean {
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  if (aKeys.length !== bKeys.length) return false;
  for (const key of aKeys) {
    if (a[key] !== b[key]) return false;
  }
  return true;
}

export function usePersistedFilters<T extends Record<string, unknown>>(
  key: string,
  defaults: T,
): PersistedFilters<T> {
  const [filters, setFilters] = useState<T>(() => {
    const stored = readPersisted<unknown>(key, defaults);
    if (!isPlainObject(stored)) return defaults;
    // Merge over defaults so adding a new filter field later doesn't leave
    // the value `undefined` from a stale stored payload. Accept any JSON
    // primitive (string | number | boolean | null) for known keys; reject
    // objects/arrays since the filter shape is intentionally flat. Union
    // types like `number | ""` are common in this codebase, so a strict
    // typeof match against the default is too aggressive.
    const merged = { ...defaults } as T;
    for (const k of Object.keys(defaults) as (keyof T)[]) {
      const incoming = (stored as Record<string, unknown>)[k as string];
      if (incoming === undefined) continue;
      const t = typeof incoming;
      if (
        incoming === null ||
        t === "string" ||
        t === "number" ||
        t === "boolean"
      ) {
        (merged[k] as unknown) = incoming;
      }
    }
    return merged;
  });

  const set = useCallback(
    (patch: Partial<T>) => {
      // Compute next from latest in the updater to coalesce back-to-back
      // calls correctly. Persistence runs as a side-effect of the updater;
      // the write is idempotent so a strict-mode double-invoke is harmless.
      setFilters((prev) => {
        const next = { ...prev, ...patch } as T;
        writePersisted(key, next);
        return next;
      });
    },
    [key],
  );

  const setField = useCallback(
    <K extends keyof T>(field: K, value: T[K]) => {
      set({ [field]: value } as unknown as Partial<T>);
    },
    [set],
  );

  const reset = useCallback(() => {
    setFilters(defaults);
    clearPersisted(key);
  }, [key, defaults]);

  const isDefault = shallowEqual(filters, defaults);

  return { filters, set, setField, reset, isDefault };
}
