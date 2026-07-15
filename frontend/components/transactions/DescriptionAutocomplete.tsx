"use client";

import {
  ChangeEvent,
  KeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { apiFetch } from "@/lib/api";
import { input } from "@/lib/styles";

/**
 * Description autocomplete (L3.2 Wave 2A).
 *
 * Wraps the single-transaction add/edit form's description input with
 * a typeahead. Server contract:
 *   GET /api/v1/transactions/suggestions/descriptions
 *     ?type=...&q=...&limit=...
 *
 * Frontend rules (frozen at
 * ~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md §5.3):
 *   - 300 ms debounce.
 *   - Skip fetch when query length < 2 (the server would 422 it).
 *   - Render up to 8 entries (server caps at 25 but the UX dropdown
 *     stays compact).
 *   - When a suggestion is picked, callers may take its category_id to
 *     pre-populate their own category selector.
 *
 * Accessibility (W3C combobox pattern):
 *   - input has role="combobox", aria-expanded, aria-controls,
 *     aria-activedescendant.
 *   - listbox uses role="listbox"; options use role="option".
 *   - Arrow keys + Enter + Escape are wired.
 *   - aria-live="polite" region announces result counts to screen
 *     readers without stealing focus.
 */

export type DescriptionSuggestion = {
  description: string;
  category_id: number;
  category_name: string;
  use_count: number;
  last_used: string;
};

type ApiResponse = { suggestions: DescriptionSuggestion[] };

export interface DescriptionAutocompleteProps {
  id: string;
  type: "income" | "expense" | "transfer";
  value: string;
  onChange: (next: string) => void;
  onPick?: (s: DescriptionSuggestion) => void;
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
  className?: string;
  ariaLabel?: string;
  /** Override fetch in tests. Receives an AbortSignal so tests can
   *  observe the cancel path the production fetch uses. */
  fetcher?: (
    type: DescriptionAutocompleteProps["type"],
    q: string,
    signal: AbortSignal,
  ) => Promise<DescriptionSuggestion[]>;
  /** Override the debounce in tests (default 300ms). */
  debounceMs?: number;
  /** Max items rendered in the dropdown (default 8). */
  maxItems?: number;
}

const DEFAULT_DEBOUNCE_MS = 300;
const MIN_QUERY_LENGTH = 2;
const DEFAULT_MAX_ITEMS = 8;

async function defaultFetcher(
  type: DescriptionAutocompleteProps["type"],
  q: string,
  signal: AbortSignal,
): Promise<DescriptionSuggestion[]> {
  const params = new URLSearchParams({ type, q, limit: "25" });
  const data = await apiFetch<ApiResponse>(
    `/api/v1/transactions/suggestions/descriptions?${params}`,
    { signal },
  );
  return data.suggestions ?? [];
}

/** True when an error came from a cancelled fetch (DOMException of name
 *  ``AbortError``). Used to silently swallow expected cancellations
 *  without conflating them with real network failures. */
function isAbortError(e: unknown): boolean {
  if (e instanceof DOMException && e.name === "AbortError") return true;
  if (
    typeof e === "object" &&
    e !== null &&
    "name" in e &&
    (e as { name?: string }).name === "AbortError"
  ) {
    return true;
  }
  return false;
}

export default function DescriptionAutocomplete({
  id,
  type,
  value,
  onChange,
  onPick,
  placeholder,
  required,
  disabled,
  className = input,
  ariaLabel,
  fetcher = defaultFetcher,
  debounceMs = DEFAULT_DEBOUNCE_MS,
  maxItems = DEFAULT_MAX_ITEMS,
}: DescriptionAutocompleteProps) {
  const listboxId = useId();
  const [suggestions, setSuggestions] = useState<DescriptionSuggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const [loading, setLoading] = useState(false);
  const [announce, setAnnounce] = useState("");
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  // Debounced fetch on value change.
  //
  // Each effect invocation owns one AbortController. The cleanup
  // function (running on every dependency change, including when the
  // user's input drops back below the 2-char minimum) aborts both the
  // debounce timer AND any in-flight fetch. This is what prevents a
  // stale response from re-opening the dropdown after the user has
  // already cleared their query: the cancelled fetch rejects with an
  // AbortError, which our catch swallows without touching state.
  useEffect(() => {
    if (disabled) return;
    if (!value || value.trim().length < MIN_QUERY_LENGTH) {
      // Below the minimum: clear visible suggestions immediately.
      // Any prior in-flight fetch was already aborted by the previous
      // cleanup before this effect re-ran.
      // eslint-disable-next-line react-hooks/set-state-in-effect -- clears the suggestion list when the query drops below the minimum length
      setSuggestions([]);
      setHighlightIdx(-1);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setLoading(true);
      try {
        const list = await fetcher(type, value.trim(), controller.signal);
        // Double-guard: even when a request is in flight at the moment
        // the user mutates the input, ``controller.signal.aborted``
        // becomes true before any later setState lands. We never
        // commit results from an aborted request.
        if (controller.signal.aborted) return;
        setSuggestions(list.slice(0, maxItems));
        setHighlightIdx(list.length > 0 ? 0 : -1);
        setOpen(true);
        setAnnounce(
          list.length === 0
            ? "No suggestions"
            : `${list.length} ${list.length === 1 ? "suggestion" : "suggestions"} available`,
        );
      } catch (err) {
        if (isAbortError(err) || controller.signal.aborted) {
          // Expected cancellation. No state update; the next effect
          // run (or the input-below-minimum branch above) owns the
          // dropdown's new state.
          return;
        }
        // Real failure: silently clear. Autocomplete is non-critical
        // UX; the field still accepts free-form input.
        setSuggestions([]);
        setHighlightIdx(-1);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, debounceMs);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [value, type, debounceMs, fetcher, maxItems, disabled]);

  // Close on click outside.
  useEffect(() => {
    function handle(e: MouseEvent) {
      if (!wrapperRef.current) return;
      if (!wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, []);

  const commitPick = useCallback(
    (s: DescriptionSuggestion) => {
      onChange(s.description);
      onPick?.(s);
      setOpen(false);
      setSuggestions([]);
      setHighlightIdx(-1);
    },
    [onChange, onPick],
  );

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!open || suggestions.length === 0) {
      if (e.key === "ArrowDown" && suggestions.length > 0) {
        setOpen(true);
        setHighlightIdx(0);
        e.preventDefault();
      }
      return;
    }
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setHighlightIdx((i) => (i + 1) % suggestions.length);
        break;
      case "ArrowUp":
        e.preventDefault();
        setHighlightIdx((i) =>
          i <= 0 ? suggestions.length - 1 : i - 1,
        );
        break;
      case "Enter":
        if (highlightIdx >= 0 && highlightIdx < suggestions.length) {
          e.preventDefault();
          commitPick(suggestions[highlightIdx]);
        }
        break;
      case "Escape":
        e.preventDefault();
        setOpen(false);
        setHighlightIdx(-1);
        break;
      case "Tab":
        // Close the popup but let focus move naturally.
        setOpen(false);
        break;
    }
  };

  const handleInput = (e: ChangeEvent<HTMLInputElement>) => {
    onChange(e.target.value);
    if (e.target.value.trim().length < MIN_QUERY_LENGTH) {
      setOpen(false);
    }
  };

  const handleFocus = () => {
    if (suggestions.length > 0) setOpen(true);
  };

  const activeId =
    open && highlightIdx >= 0 ? `${listboxId}-opt-${highlightIdx}` : undefined;

  return (
    <div ref={wrapperRef} className="relative">
      <input
        id={id}
        type="text"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open && suggestions.length > 0}
        aria-controls={listboxId}
        aria-activedescendant={activeId}
        aria-label={ariaLabel}
        autoComplete="off"
        required={required}
        disabled={disabled}
        value={value}
        placeholder={placeholder}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        onFocus={handleFocus}
        className={className}
      />
      {open && suggestions.length > 0 && (
        <ul
          id={listboxId}
          role="listbox"
          aria-label="Description suggestions"
          className="absolute z-20 mt-1 max-h-64 w-full overflow-auto rounded-md border border-border bg-surface-raised shadow-lg"
        >
          {suggestions.map((s, i) => (
            <li
              key={`${s.description}-${i}`}
              id={`${listboxId}-opt-${i}`}
              role="option"
              aria-selected={i === highlightIdx}
              onMouseDown={(e) => {
                // mousedown (not click) so we run before the input
                // loses focus and closes the dropdown.
                e.preventDefault();
                commitPick(s);
              }}
              onMouseEnter={() => setHighlightIdx(i)}
              className={`cursor-pointer px-3 py-2 text-sm ${
                i === highlightIdx
                  ? "bg-accent/10 text-text-primary"
                  : "text-text-primary"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate">{s.description}</span>
                <span className="shrink-0 text-[10px] text-text-muted">
                  {s.category_name}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {/* Polite live region: announces result counts to AT users
          without stealing keyboard focus. */}
      <div role="status" aria-live="polite" className="sr-only">
        {loading ? "Loading suggestions" : announce}
      </div>
    </div>
  );
}
