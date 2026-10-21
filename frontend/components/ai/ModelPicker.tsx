"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { btnLink, input } from "@/lib/styles";

interface ModelEntry {
  id: string;
  label: string | null;
  hint: string | null;
  tier: "budget" | "standard" | "frontier" | null;
  replies_left: number | null;
}

interface ModelList {
  default: string | null;
  replies_period: "day" | "month" | null;
  models: ModelEntry[];
}

const TIER_WORD = { budget: "Budget", standard: "Standard", frontier: "Frontier" } as const;

interface Props {
  id: string;
  credentialId: number | "";
  /** Per-feature rows pass their key; the default row omits it. */
  feature?: string;
  /** Platform credentials have no refresh (validate answers 409). */
  platform?: boolean;
  value: string;
  onChange: (model: string) => void;
  /** True when `value` is one of the listed ids. Callers gate Save on it. */
  onListedChange?: (listed: boolean) => void;
}

/**
 * Combobox over the models a saved key can use (TBD-608). The value is only
 * ever a listed id or the untouched stale saved id: typed text filters, it
 * never becomes the value. Server order is kept; the client does not sort.
 */
export default function ModelPicker({
  id,
  credentialId,
  feature,
  platform = false,
  value,
  onChange,
  onListedChange,
}: Props) {
  const [list, setList] = useState<ModelList | null>(null);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(-1);
  const ref = useRef<HTMLDivElement>(null);
  const valueRef = useRef(value);
  valueRef.current = value;
  const lastCred = useRef<number | "">(credentialId);
  const seq = useRef(0);

  const load = useCallback(
    async (cred: number, afterValidate: boolean) => {
      const mine = ++seq.current;
      setLoading(true);
      setFetchError(null);
      try {
        if (afterValidate) {
          await apiFetch(`/api/v1/settings/ai-providers/${cred}/validate`, { method: "POST" });
        }
        const qs = feature ? `?feature=${encodeURIComponent(feature)}` : "";
        const res = await apiFetch<ModelList>(`/api/v1/settings/ai-providers/${cred}/models${qs}`);
        if (mine !== seq.current) return;
        setList(res);
        const changed = lastCred.current !== cred;
        lastCred.current = cred;
        if (changed || !valueRef.current) onChange(res.default ?? "");
      } catch (err) {
        if (mine !== seq.current) return;
        setList(null);
        setFetchError(extractErrorMessage(err, "Could not load models"));
      } finally {
        if (mine === seq.current) setLoading(false);
      }
    },
    // onChange is a state setter or stable wrapper at every call site
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [feature],
  );

  useEffect(() => {
    if (credentialId === "") {
      seq.current++;
      // eslint-disable-next-line react-hooks/set-state-in-effect -- no credential means no list; drop the previous key's models
      setList(null);
      setLoading(false);
      setFetchError(null);
      return;
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch the model list when the credential or feature changes
    load(credentialId, false);
  }, [credentialId, load]);

  const listed = !!list && value !== "" && list.models.some((m) => m.id === value);
  useEffect(() => { onListedChange?.(listed); }, [listed, onListedChange]);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery("");
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const q = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    const models = list?.models ?? [];
    return q
      ? models.filter((m) => m.id.toLowerCase().includes(q) || (m.label?.toLowerCase().includes(q) ?? false))
      : models;
  }, [list, q]);

  function pick(m: ModelEntry) {
    onChange(m.id);
    setQuery("");
    setOpen(false);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter") { setOpen(true); e.preventDefault(); }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlight >= 0 && highlight < filtered.length) pick(filtered[highlight]);
    } else if (e.key === "Escape") {
      setOpen(false);
      setQuery("");
    }
  }

  const empty = !!list && list.models.length === 0;
  const stale = !!list && value !== "" && !listed;
  const disabled = credentialId === "" || loading || empty || (!list && !fetchError);
  const placeholder =
    credentialId === "" ? "Choose a credential first" : loading ? "Loading models…" : "Search models...";
  const periodWord = list?.replies_period === "day" ? "today" : "this month";

  return (
    <div ref={ref} className="relative">
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="text"
          autoComplete="off"
          className={input}
          role="combobox"
          aria-expanded={open}
          aria-haspopup="listbox"
          aria-autocomplete="list"
          aria-controls={`${id}-listbox`}
          aria-activedescendant={open && highlight >= 0 ? `${id}-opt-${highlight}` : undefined}
          disabled={disabled}
          placeholder={placeholder}
          value={open ? query : value}
          onChange={(e) => { setQuery(e.target.value); setHighlight(-1); if (!open) setOpen(true); }}
          onFocus={() => { setOpen(true); setQuery(""); setHighlight(-1); }}
          onKeyDown={onKeyDown}
        />
        {!platform && credentialId !== "" && (
          <button
            type="button"
            className={`${btnLink} whitespace-nowrap`}
            disabled={refreshing}
            onClick={async () => {
              setRefreshing(true);
              try { await load(credentialId, true); } finally { setRefreshing(false); }
            }}
          >
            {refreshing ? "Refreshing…" : "Refresh models"}
          </button>
        )}
      </div>

      {open && !disabled && (
        <div
          role="listbox"
          id={`${id}-listbox`}
          className="absolute z-50 mt-1 max-h-64 w-full overflow-y-auto rounded-md border border-border bg-surface shadow-lg"
        >
          {filtered.map((m, i) => (
            <button
              key={m.id}
              type="button"
              role="option"
              id={`${id}-opt-${i}`}
              aria-selected={m.id === value}
              onClick={() => pick(m)}
              className={`block w-full px-3 py-2 text-left text-sm transition-colors ${
                highlight === i ? "bg-accent-dim text-accent" : "text-text-primary hover:bg-surface-raised"
              }`}
            >
              <span className="flex items-center justify-between gap-2">
                <span>
                  {m.label ?? m.id}
                  {m.id === list?.default && (
                    <span className="ml-1.5 text-[11px] text-text-muted">Default</span>
                  )}
                </span>
                {m.tier && <span className="text-xs text-text-muted">{TIER_WORD[m.tier]}</span>}
              </span>
              {m.hint && <span className="block text-xs text-text-muted">{m.hint}</span>}
              {m.replies_left != null && (
                <span className="block text-xs text-text-muted">
                  about {m.replies_left.toLocaleString()} replies left {periodWord}
                </span>
              )}
            </button>
          ))}
        </div>
      )}

      {empty && (
        <p className="mt-1 text-xs text-text-muted">
          This key has no model that can do this. Refresh models or pick another key.
        </p>
      )}
      {stale && (
        <p className="mt-1 text-xs text-warning">
          No longer in this key&apos;s list. Pick another to change it.
        </p>
      )}
      {fetchError && (
        <p className="mt-1 text-xs text-danger">
          {fetchError}{" "}
          <button
            type="button"
            className={btnLink}
            onClick={() => credentialId !== "" && load(credentialId, false)}
          >
            Retry
          </button>
        </p>
      )}
    </div>
  );
}
