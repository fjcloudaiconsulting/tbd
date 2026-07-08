"use client";

/**
 * Tag filter — chip picker over the org's tags. Suggestions are
 * fetched on mount via ``GET /api/v1/tags``. Selecting a tag adds it
 * to the chip strip; clicking an active chip removes it. A radio
 * row below the chip input toggles ``tag_match`` between "all"
 * (every tag must be present) and "any" (at least one tag must be
 * present), mirroring the transactions-list contract.
 *
 * Spec section 4: default is ``all``. The architect-locked tag-match
 * inversion bug PR2 fixed lives at the wire layer (one ``in`` filter
 * carrying the whole list) — this picker only shapes the user-facing
 * UI. ``resolveFilters`` still emits the correct AST.
 */
import { useId } from "react";

import { useAuth } from "@/components/auth/AuthProvider";
import { useTags } from "@/lib/hooks/use-tags";
import type { TagMatch } from "@/lib/reports/types";

interface Props {
  value: string[];
  match: TagMatch;
  onChange: (next: { tag_names: string[]; tag_match: TagMatch }) => void;
  /** Label shown above the chip strip. */
  label?: string;
  /** Aria-prefix on chip toggle buttons. */
  ariaPrefix?: string;
}

export default function TagFilter({
  value,
  match,
  onChange,
  label = "Tags",
  ariaPrefix = "Tag",
}: Props) {
  // Share the org tags cache via the bare-path `useTags` hook, auth-gated
  // (`!loading && !!user`) like the other reference-data consumers.
  const { user, loading } = useAuth();
  const { data, error, isLoading } = useTags(!loading && !!user);

  const radioName = useId();
  const tags = data ?? [];
  const selectedSet = new Set(value);

  function toggle(name: string) {
    const next = selectedSet.has(name)
      ? value.filter((n) => n !== name)
      : [...value, name];
    onChange({ tag_names: next, tag_match: match });
  }

  function setMatch(next: TagMatch) {
    onChange({ tag_names: value, tag_match: next });
  }

  return (
    <div className="flex flex-col gap-1.5" data-testid="tag-filter">
      <span className="text-[10px] font-medium uppercase tracking-wider text-text-muted">
        {label}
      </span>
      {error ? (
        <div
          role="alert"
          data-testid="tag-filter-error"
          className="text-xs text-danger"
        >
          Couldn&apos;t load tags
        </div>
      ) : isLoading ? (
        <div
          data-testid="tag-filter-loading"
          className="h-6 w-28 animate-pulse rounded bg-border/40"
        />
      ) : tags.length === 0 ? (
        <span className="text-xs text-text-muted">No tags yet</span>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {tags.map((t) => {
            const active = selectedSet.has(t.name);
            return (
              <button
                key={t.id}
                type="button"
                data-testid={`tag-filter-chip-${t.name}`}
                aria-pressed={active}
                aria-label={`${ariaPrefix} ${t.name}`}
                onClick={() => toggle(t.name)}
                className={`rounded-full border px-2.5 py-0.5 text-xs transition ${
                  active
                    ? "border-accent bg-accent text-accent-text"
                    : "border-border text-text-secondary hover:bg-surface-raised"
                }`}
              >
                {t.name}
              </button>
            );
          })}
        </div>
      )}
      <div className="mt-1 flex gap-3 text-xs text-text-secondary">
        <label className="flex items-center gap-1">
          <input
            type="radio"
            name={radioName}
            data-testid="tag-filter-match-all"
            aria-label="Tag match all"
            checked={match === "all"}
            onChange={() => setMatch("all")}
          />
          <span>Match all</span>
        </label>
        <label className="flex items-center gap-1">
          <input
            type="radio"
            name={radioName}
            data-testid="tag-filter-match-any"
            aria-label="Tag match any"
            checked={match === "any"}
            onChange={() => setMatch("any")}
          />
          <span>Match any</span>
        </label>
      </div>
    </div>
  );
}
