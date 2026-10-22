"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { ApiResponseError, apiFetch, extractErrorMessage } from "@/lib/api";
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
const LIST_MAX_PX = 256;

interface Props {
  id: string;
  credentialId: number | "";
  /** Per-feature rows pass their key; the default row omits it. */
  feature?: string;
  /** Platform credentials have no refresh (validate answers 409). */
  platform?: boolean;
  /** The credential is not on the page's list: no refresh, and not claimed to be Included AI. */
  unknown?: boolean;
  value: string;
  onChange: (model: string) => void;
  /** True when `value` is one of the listed ids. Callers gate Save on it. */
  onListedChange?: (listed: boolean) => void;
  /** Accessible name for the field when no visible <label> points at it. */
  "aria-label"?: string;
  /** Row name used to tell the Refresh and Retry buttons apart. */
  rowLabel?: string;
  /** Called after a successful validate, so the page can refetch credential state. */
  onValidated?: () => void;
}

/**
 * Combobox over the models a saved key can use (TBD-608). The value is only
 * ever a listed id or the untouched stale saved id: typed text filters, it
 * never becomes the value. Server order is kept; the client does not sort.
 * The listbox is portalled with position: fixed so the settings table's
 * overflow-x-auto wrapper cannot clip it.
 */
export default function ModelPicker({
  id,
  credentialId,
  feature,
  platform = false,
  unknown = false,
  value,
  onChange,
  onListedChange,
  "aria-label": ariaLabel,
  rowLabel,
  onValidated,
}: Props) {
  const [list, setList] = useState<ModelList | null>(null);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(-1);
  const [pos, setPos] = useState<CSSProperties | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const valueRef = useRef(value);
  useEffect(() => { valueRef.current = value; }, [value]);
  const onValidatedRef = useRef(onValidated);
  useEffect(() => { onValidatedRef.current = onValidated; }, [onValidated]);
  const lastCred = useRef<number | "">(credentialId);
  const seq = useRef(0);

  const load = useCallback(
    async (cred: number, afterValidate: boolean) => {
      const mine = ++seq.current;
      setLoading(true);
      setFetchError(null);
      // A refresh keeps the list it has until the new one lands (or fails).
      if (!afterValidate) setList(null);
      try {
        if (afterValidate) {
          await apiFetch(`/api/v1/settings/ai-providers/${cred}/validate`, { method: "POST" });
          if (mine !== seq.current) return;
          onValidatedRef.current?.();
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
        setFetchError(
          err instanceof ApiResponseError && (err.code === "platform_ai_unavailable" || err.status === 402)
            ? "Included AI is not available right now."
            : extractErrorMessage(err, "Could not load models"),
        );
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

  const q = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    const models = list?.models ?? [];
    return q
      ? models.filter((m) => m.id.toLowerCase().includes(q) || (m.label?.toLowerCase().includes(q) ?? false))
      : models;
  }, [list, q]);

  const showList = open && !!list;

  useLayoutEffect(() => {
    if (!showList) return;
    function place() {
      const r = inputRef.current?.getBoundingClientRect();
      if (!r) return;
      const below = window.innerHeight - r.bottom;
      const flip = below < LIST_MAX_PX && r.top > below;
      setPos({
        left: r.left,
        width: r.width,
        ...(flip ? { bottom: window.innerHeight - r.top + 4 } : { top: r.bottom + 4 }),
        maxHeight: Math.max(96, Math.min(LIST_MAX_PX, (flip ? r.top : below) - 12)),
      });
    }
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [showList]);

  useEffect(() => {
    if (highlight < 0) return;
    (listRef.current?.children[highlight] as HTMLElement | undefined)?.scrollIntoView?.({ block: "nearest" });
  }, [highlight]);

  function close() {
    setOpen(false);
    setQuery("");
    setHighlight(-1);
  }

  function pick(m: ModelEntry) {
    onChange(m.id);
    close();
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter") { setOpen(true); e.preventDefault(); }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight(Math.min(highlight + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight(Math.max(highlight - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlight >= 0) pick(filtered[highlight]);
    } else if (e.key === "Escape") {
      close();
    }
  }

  const empty = !!list && list.models.length === 0;
  const stale = !!list && value !== "" && !listed;
  const disabled = credentialId === "" || loading || empty || (!list && !fetchError);
  const placeholder =
    credentialId === "" ? "Choose a credential first" : loading ? "Loading models…" : "Search models...";
  const periodWord = list?.replies_period === "day" ? "today" : "this month";
  const noteIds = [
    loading && credentialId !== "" ? `${id}-loading` : null,
    empty ? `${id}-empty` : null,
    stale ? `${id}-stale` : null,
    fetchError ? `${id}-error` : null,
  ].filter(Boolean);
  const named = (what: string) => (rowLabel ? `${what} for ${rowLabel}` : undefined);

  return (
    <div className="relative">
      <div className="flex items-center gap-2">
        <input
          ref={inputRef}
          id={id}
          type="text"
          autoComplete="off"
          className={input}
          role="combobox"
          aria-label={ariaLabel}
          aria-expanded={showList}
          aria-haspopup="listbox"
          aria-autocomplete="list"
          aria-controls={`${id}-listbox`}
          aria-activedescendant={showList && highlight >= 0 ? `${id}-opt-${highlight}` : undefined}
          aria-describedby={noteIds.length ? noteIds.join(" ") : undefined}
          disabled={disabled}
          placeholder={placeholder}
          value={open ? query : value}
          onChange={(e) => { setQuery(e.target.value); setHighlight(-1); if (!open) setOpen(true); }}
          onFocus={() => { setOpen(true); setQuery(""); setHighlight(-1); }}
          onBlur={close}
          onKeyDown={onKeyDown}
        />
        {!platform && !unknown && credentialId !== "" && (
          <button
            type="button"
            className={`${btnLink} whitespace-nowrap`}
            aria-label={named("Refresh models")}
            disabled={refreshing}
            onClick={async () => {
              close();
              setRefreshing(true);
              try { await load(credentialId, true); } finally { setRefreshing(false); }
            }}
          >
            {refreshing ? "Refreshing…" : "Refresh models"}
          </button>
        )}
      </div>

      {showList &&
        createPortal(
          <div
            ref={listRef}
            role="listbox"
            id={`${id}-listbox`}
            style={{ position: "fixed", ...pos }}
            // keep focus in the input so a click on an option never fires its blur
            onMouseDown={(e) => e.preventDefault()}
            className="z-50 overflow-y-auto rounded-md border border-border bg-surface shadow-lg"
          >
            {filtered.length === 0 && (
              <div className="px-3 py-4 text-center text-sm text-text-muted">No models match</div>
            )}
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
          </div>,
          document.body,
        )}

      {loading && credentialId !== "" && (
        <p id={`${id}-loading`} role="status" className="mt-1 text-xs text-text-muted">
          Loading models…
        </p>
      )}
      {empty && (
        <p id={`${id}-empty`} role="status" className="mt-1 text-xs text-text-muted">
          {platform
            ? "Included AI has no model for this right now."
            : unknown
              ? "This key has no model that can do this."
              : "This key has no model that can do this. Refresh models or pick another key."}
        </p>
      )}
      {stale && (
        <p id={`${id}-stale`} role="status" className="mt-1 text-xs text-warning">
          No longer in this key&apos;s list. Pick another to change it.
        </p>
      )}
      {fetchError && (
        <p id={`${id}-error`} role="alert" className="mt-1 text-xs text-danger">
          {fetchError}{" "}
          <button
            type="button"
            className={btnLink}
            aria-label={named("Retry")}
            onClick={() => credentialId !== "" && load(credentialId, false)}
          >
            Retry
          </button>
        </p>
      )}
    </div>
  );
}
