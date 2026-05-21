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
 * Tag chip input for transaction add/edit forms (PR-Tags-A frontend).
 *
 * Wires the backend tag autocomplete contract:
 *   GET /api/v1/tags/suggest?prefix=...&category_id=...&limit=...
 *
 * Behavior:
 *   - User types -> debounced fetch (200ms). Minimum prefix length 1.
 *   - Enter / Tab / comma commits the typed text OR the currently-
 *     highlighted autocomplete suggestion (whichever is active). The
 *     transactions submit path posts the chip names to
 *     PUT /api/v1/transactions/{id}/tags which auto-creates any tags
 *     the org has not used before.
 *   - Space ALWAYS commits the typed draft (never the highlighted
 *     suggestion). Space is the most discoverable "add another tag"
 *     shortcut for users coming from social platforms; routing it
 *     past the suggestion list keeps the behavior intuitive — typing
 *     "bike[space]" commits "bike" even if "biking" is highlighted.
 *     Multi-word tags from the keyboard are no longer typeable; use
 *     a hyphen ("bike-commute") or click a suggestion that already
 *     contains a space.
 *   - Backspace on an empty input removes the last chip.
 *   - Click suggestion commits chip.
 *   - Each chip carries a remove "x" with aria-label="Remove tag <name>".
 *   - Cap (MAX_TAGS_PER_TRANSACTION = 5) is enforced client-side; backend
 *     also rejects with 422.
 *   - Names normalize lowercase + collapsed whitespace + [a-z0-9 -] on
 *     the server. We surface a friendly inline error when the server
 *     rejects.
 *
 * Accessibility (W3C combobox pattern, mirrors DescriptionAutocomplete):
 *   - input role="combobox", aria-expanded, aria-controls,
 *     aria-activedescendant.
 *   - listbox role="listbox"; options role="option".
 *   - aria-live polite region announces suggestion counts.
 *   - Chip remove buttons return focus to the input.
 */

/** Hard frontend cap. Mirrors backend MAX_TAGS_PER_TRANSACTION in
 *  app/schemas/tag.py. Bumping requires both ends to change. */
export const MAX_TAGS_PER_TRANSACTION = 5;

/** Server-side normalize set: lowercase, collapsed whitespace, allowed
 *  characters [a-z0-9 -]. Length cap 32. */
export const TAG_NAME_MAX_LENGTH = 32;

export type TagSuggestion = {
  name: string;
  source: "org_co_category" | "org_recent" | "shared_dictionary";
  weight: number;
};

type SuggestApiResponse = { suggestions: TagSuggestion[] };

export interface TagChipInputProps {
  /** Currently attached tag names (lowercase / normalized client-side). */
  value: string[];
  /** Called whenever the chip list changes. */
  onChange: (next: string[]) => void;
  /** Currently selected category for the transaction (drives the
   *  "tags used on this category" suggestion pass). Pass null/"" when
   *  the form has no category picked yet. */
  categoryId?: number | "" | null;
  /** DOM id for the input (label htmlFor). */
  id: string;
  /** Accessibility label override (default: "Tags"). */
  ariaLabel?: string;
  /** Disable interaction (form submitting, etc.). */
  disabled?: boolean;
  /** Override fetch in tests. */
  fetcher?: (
    prefix: string,
    categoryId: number | null,
    signal: AbortSignal,
  ) => Promise<TagSuggestion[]>;
  /** Override the debounce in tests (default 200ms). */
  debounceMs?: number;
  /** Override the cap in tests (default MAX_TAGS_PER_TRANSACTION). */
  maxTags?: number;
}

const DEFAULT_DEBOUNCE_MS = 200;
const SUGGEST_LIMIT = 10;

async function defaultFetcher(
  prefix: string,
  categoryId: number | null,
  signal: AbortSignal,
): Promise<TagSuggestion[]> {
  const params = new URLSearchParams({ limit: String(SUGGEST_LIMIT) });
  if (prefix) params.set("prefix", prefix);
  if (categoryId != null) params.set("category_id", String(categoryId));
  const data = await apiFetch<SuggestApiResponse>(
    `/api/v1/tags/suggest?${params}`,
    { signal },
  );
  return data.suggestions ?? [];
}

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

/** Client-side normalize: lowercase, collapse internal whitespace,
 *  strip leading/trailing whitespace, and drop characters the server
 *  would refuse. This keeps the chip list stable across the wire round
 *  trip even when the server normalizes more aggressively. */
function normalizeName(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9 \-]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export default function TagChipInput({
  value,
  onChange,
  categoryId = null,
  id,
  ariaLabel = "Tags",
  disabled,
  fetcher = defaultFetcher,
  debounceMs = DEFAULT_DEBOUNCE_MS,
  maxTags = MAX_TAGS_PER_TRANSACTION,
}: TagChipInputProps) {
  const listboxId = useId();
  const [draft, setDraft] = useState("");
  const [suggestions, setSuggestions] = useState<TagSuggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const [loading, setLoading] = useState(false);
  const [announce, setAnnounce] = useState("");
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  const atCap = value.length >= maxTags;
  // Normalize the category prop into a number-or-null so the fetcher
  // sees a stable shape even when callers pass "" from form state.
  const categoryIdNum =
    categoryId === "" || categoryId == null ? null : categoryId;

  const commitChip = useCallback(
    (raw: string) => {
      const name = normalizeName(raw);
      if (!name) return;
      if (name.length > TAG_NAME_MAX_LENGTH) {
        setError(`Tag must be ${TAG_NAME_MAX_LENGTH} characters or fewer`);
        return;
      }
      if (value.includes(name)) {
        // Already attached. Clear the draft silently so the user can
        // keep typing the next tag without a confusing duplicate
        // chip.
        setDraft("");
        setOpen(false);
        setHighlightIdx(-1);
        return;
      }
      if (value.length >= maxTags) {
        setError(`Maximum ${maxTags} tags per transaction`);
        return;
      }
      setError("");
      onChange([...value, name]);
      setDraft("");
      setOpen(false);
      setHighlightIdx(-1);
    },
    [maxTags, onChange, value],
  );

  const removeChip = useCallback(
    (name: string) => {
      onChange(value.filter((t) => t !== name));
      setError("");
      // Return focus to the input so the user keeps the keyboard
      // flow after pressing x on a chip.
      window.setTimeout(() => inputRef.current?.focus(), 0);
    },
    [onChange, value],
  );

  // Debounced fetch on draft change.
  useEffect(() => {
    if (disabled) return;
    if (atCap) {
      setSuggestions([]);
      setOpen(false);
      setHighlightIdx(-1);
      return;
    }
    const trimmed = draft.trim();
    if (!trimmed) {
      setSuggestions([]);
      setHighlightIdx(-1);
      setOpen(false);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setLoading(true);
      try {
        const list = await fetcher(trimmed, categoryIdNum, controller.signal);
        if (controller.signal.aborted) return;
        // Filter out names that are already chipped — no point
        // showing them.
        const filtered = list.filter((s) => !value.includes(s.name));
        setSuggestions(filtered);
        setHighlightIdx(filtered.length > 0 ? 0 : -1);
        setOpen(true);
        setAnnounce(
          filtered.length === 0
            ? "No suggestions"
            : `${filtered.length} ${
                filtered.length === 1 ? "suggestion" : "suggestions"
              } available`,
        );
      } catch (err) {
        if (isAbortError(err) || controller.signal.aborted) return;
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
  }, [draft, categoryIdNum, debounceMs, fetcher, value, disabled, atCap]);

  // Click outside closes the dropdown.
  useEffect(() => {
    function handle(e: MouseEvent) {
      if (!wrapperRef.current) return;
      if (!wrapperRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, []);

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    // Space is the "commit my typed draft now" shortcut. Routed
    // separately from Enter / Tab / comma so it NEVER accepts the
    // currently-highlighted suggestion — pressing space conceptually
    // ends the word the user is typing, not the word autocomplete is
    // offering. Architect call 2026-05-21 after the PR #334 review.
    //
    // Always preventDefault so a real browser doesn't insert a literal
    // space character into the input (which the test harness can't
    // observe via fireEvent.keyDown).
    if (e.key === " ") {
      e.preventDefault();
      if (draft.trim()) {
        commitChip(draft);
      }
      return;
    }

    if (e.key === "Enter" || e.key === "Tab" || e.key === ",") {
      // Tab still moves focus; Enter / comma stay inside the input
      // to commit. We only intercept Tab when there's a draft so
      // empty Tab still moves to the next field.
      if (e.key === "Tab" && !draft.trim() && highlightIdx < 0) return;
      e.preventDefault();
      if (
        open &&
        highlightIdx >= 0 &&
        highlightIdx < suggestions.length
      ) {
        commitChip(suggestions[highlightIdx].name);
      } else if (draft.trim()) {
        commitChip(draft);
      }
      return;
    }
    if (e.key === "Backspace" && !draft && value.length > 0) {
      e.preventDefault();
      removeChip(value[value.length - 1]);
      return;
    }
    if (!open || suggestions.length === 0) return;
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
      case "Escape":
        e.preventDefault();
        setOpen(false);
        setHighlightIdx(-1);
        break;
    }
  };

  const handleInput = (e: ChangeEvent<HTMLInputElement>) => {
    const next = e.target.value;
    setDraft(next);
    setError("");
  };

  const handleFocus = () => {
    if (suggestions.length > 0 && draft.trim()) setOpen(true);
  };

  const activeId =
    open && highlightIdx >= 0 ? `${listboxId}-opt-${highlightIdx}` : undefined;

  return (
    <div ref={wrapperRef} className="relative">
      <div
        className={`${input} flex flex-wrap items-center gap-1.5 ${
          disabled ? "opacity-60" : ""
        }`}
        onClick={() => {
          if (!disabled) inputRef.current?.focus();
        }}
      >
        {value.map((name) => (
          <span
            key={name}
            className="inline-flex items-center gap-1 rounded bg-accent/15 px-2 py-0.5 text-xs text-text-primary"
            data-testid={`tag-chip-${name}`}
          >
            <span>{name}</span>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                removeChip(name);
              }}
              onKeyDown={(e) => {
                // Enter / Space remove the chip from keyboard. The
                // implicit Enter behavior on <button> already does this
                // via click, but Space inside a button only fires on
                // keyup — making it explicit keeps the keyboard path
                // identical for both keys.
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  removeChip(name);
                }
              }}
              disabled={disabled}
              aria-label={`Remove tag ${name}`}
              // Chip × buttons are part of the natural tab order so
              // keyboard-only users can free a slot when the input is
              // disabled at MAX_TAGS_PER_TRANSACTION. DOM order is
              // chips-before-input, so forward Tab from the previous
              // field walks chips → input → next field, and Shift+Tab
              // from the input reaches the last chip's × in reverse —
              // matching the Gmail / GitHub label-picker convention
              // the operator called out.
              className="ml-0.5 inline-flex h-4 w-4 items-center justify-center rounded text-text-muted hover:text-danger focus:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
            >
              <span aria-hidden="true">&times;</span>
            </button>
          </span>
        ))}
        <input
          id={id}
          ref={inputRef}
          type="text"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={open && suggestions.length > 0}
          aria-controls={listboxId}
          aria-activedescendant={activeId}
          aria-label={ariaLabel}
          autoComplete="off"
          disabled={disabled || atCap}
          value={draft}
          placeholder={
            atCap
              ? `Maximum ${maxTags} tags`
              : value.length === 0
                ? "Add a tag"
                : ""
          }
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          onFocus={handleFocus}
          maxLength={TAG_NAME_MAX_LENGTH}
          className="flex-1 min-w-[100px] bg-transparent text-sm text-text-primary placeholder:text-text-muted focus:outline-none disabled:cursor-not-allowed"
        />
      </div>

      {open && suggestions.length > 0 && (
        <ul
          id={listboxId}
          role="listbox"
          aria-label="Tag suggestions"
          className="absolute z-20 mt-1 max-h-64 w-full overflow-auto rounded-md border border-border bg-surface-raised shadow-lg"
        >
          {suggestions.map((s, i) => (
            <li
              key={s.name}
              id={`${listboxId}-opt-${i}`}
              role="option"
              aria-selected={i === highlightIdx}
              onMouseDown={(e) => {
                // mousedown (not click) so we run before the input
                // blurs and closes the dropdown.
                e.preventDefault();
                commitChip(s.name);
              }}
              onMouseEnter={() => setHighlightIdx(i)}
              className={`cursor-pointer px-3 py-2 text-sm ${
                i === highlightIdx
                  ? "bg-accent/10 text-text-primary"
                  : "text-text-primary"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate">{s.name}</span>
                {s.source === "shared_dictionary" && (
                  <span className="shrink-0 text-[10px] text-text-muted">
                    suggested
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {error && (
        <p
          role="alert"
          className="mt-1 text-xs text-danger"
          data-testid="tag-chip-error"
        >
          {error}
        </p>
      )}

      <div role="status" aria-live="polite" className="sr-only">
        {loading ? "Loading suggestions" : announce}
      </div>
    </div>
  );
}
