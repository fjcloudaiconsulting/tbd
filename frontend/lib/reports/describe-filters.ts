/**
 * ``describeWidgetFilters`` — turns a widget's effective filter state
 * into an ordered list of human-readable chip descriptors for the
 * per-widget filter-chip header.
 *
 * The chips must mirror exactly what ``resolveFilters`` produces so they
 * never lie about what the widget queries. To stay in lock-step, this
 * helper imports ``pickDateRange`` and ``isFieldOverridden`` from
 * ``resolve.ts`` (the single source of truth for the date inherit /
 * override decision) rather than reimplementing that logic.
 *
 * Account/category ids are resolved to names via already-fetched
 * ``accounts`` / ``categories`` lookups (the same SWR caches the filter
 * pickers warm). When no name resolves (deleted/inactive id), the chip
 * falls back to a plain count label so it never blocks on load.
 */
import { buildPresetRanges } from "@/lib/reports/date-presets";
import { isFieldOverridden, pickDateRange } from "@/lib/reports/resolve";
import type {
  CanvasDateRange,
  CanvasFilters,
  Widget,
  WidgetFilters,
} from "@/lib/reports/types";
import type { Account, Category } from "@/lib/types";

export interface FilterChip {
  key: "date" | "txn_type" | "amount" | "tags" | "accounts" | "categories";
  /** Human, truncated label, e.g. "Groceries +2". */
  label: string;
  /** Date only: true when the widget overrides the shared canvas date. */
  overridden?: boolean;
}

interface Lookups {
  accounts: Account[];
  categories: Category[];
}

export function describeWidgetFilters(
  widget: Widget,
  canvasFilters: CanvasFilters | undefined,
  lookups: Lookups,
  now?: Date,
  // When false (a date-less source such as ``accounts``), no date chip
  // is emitted — the resolver drops the date filter at query time, so a
  // chip would show but do nothing. Defaults to true (transactions and
  // the pre-catalog-load window) to preserve current behavior.
  sourceSupportsDate = true,
): FilterChip[] {
  const chips: FilterChip[] = [];

  const widgetFilters: WidgetFilters =
    "filters" in widget.config && widget.config.filters
      ? widget.config.filters
      : {};

  // ── date ──────────────────────────────────────────────────────
  const effectiveDate = pickDateRange(
    widgetFilters.date_range,
    canvasFilters?.date_range,
  );
  if (
    sourceSupportsDate &&
    effectiveDate &&
    (effectiveDate.start || effectiveDate.end)
  ) {
    chips.push({
      key: "date",
      label: dateLabel(effectiveDate, now ?? new Date()),
      overridden: isFieldOverridden("date_range", widgetFilters, canvasFilters),
    });
  }

  // ── txn_type ──────────────────────────────────────────────────
  if (widgetFilters.txn_type) {
    chips.push({ key: "txn_type", label: capitalize(widgetFilters.txn_type) });
  }

  // ── amount ────────────────────────────────────────────────────
  const amount = widgetFilters.amount_range;
  if (amount && (amount.min !== undefined || amount.max !== undefined)) {
    chips.push({ key: "amount", label: amountLabel(amount.min, amount.max) });
  }

  // ── tags ──────────────────────────────────────────────────────
  if (widgetFilters.tag_names && widgetFilters.tag_names.length > 0) {
    const base = truncatedList(widgetFilters.tag_names);
    const suffix = widgetFilters.tag_match === "any" ? " (any)" : "";
    chips.push({ key: "tags", label: `${base}${suffix}` });
  }

  // ── accounts ──────────────────────────────────────────────────
  if (widgetFilters.account_ids && widgetFilters.account_ids.length > 0) {
    chips.push({
      key: "accounts",
      label: nameLabel(
        widgetFilters.account_ids,
        lookups.accounts,
        "account",
        "accounts",
      ),
    });
  }

  // ── categories ────────────────────────────────────────────────
  if (widgetFilters.category_ids && widgetFilters.category_ids.length > 0) {
    chips.push({
      key: "categories",
      label: nameLabel(
        widgetFilters.category_ids,
        lookups.categories,
        "category",
        "categories",
      ),
    });
  }

  return chips;
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Currency symbol is a bare ``$`` prefix (not per-account currency),
// consistent with the deferred chart currency-symbol work (roadmap §1b).
function amountLabel(min?: number, max?: number): string {
  if (min !== undefined && max !== undefined) return `$${min}–$${max}`;
  if (min !== undefined) return `≥ $${min}`;
  return `≤ $${max}`;
}

// "Groceries +2" — first name plus a count of the rest. A bare list of
// strings (tags) truncates the same way.
function truncatedList(names: string[]): string {
  if (names.length === 0) return "";
  const first = names[0];
  const rest = names.length - 1;
  return rest > 0 ? `${first} +${rest}` : first;
}

// Resolve ids → names against a lookup keyed by ``id``. Shows the first
// resolved name plus a ``+N`` count of EVERY other id — resolved or not —
// so the chip never underreports how many ids the widget actually
// filters on. When NO id resolves, fall back to a plain count label
// (``"2 accounts"``) so the chip is never empty.
function nameLabel<T extends { id: number; name: string }>(
  ids: number[],
  lookup: T[],
  singular: string,
  plural: string,
): string {
  const byId = new Map(lookup.map((x) => [x.id, x.name]));
  const firstName = ids.map((id) => byId.get(id)).find((n) => n !== undefined);
  if (firstName === undefined) {
    return `${ids.length} ${ids.length === 1 ? singular : plural}`;
  }
  // ``+N`` counts every id beyond the one shown, including unresolved
  // ids, so the count matches the real number of filtered ids.
  const rest = ids.length - 1;
  return rest > 0 ? `${firstName} +${rest}` : firstName;
}

const PRESET_LABELS: Record<
  keyof ReturnType<typeof buildPresetRanges>,
  string
> = {
  this_month: "This month",
  last_month: "Last month",
  ytd: "YTD",
  last_12_months: "Last 12 months",
};

function dateLabel(range: CanvasDateRange, now: Date): string {
  const presets = buildPresetRanges(now);
  for (const key of Object.keys(presets) as Array<keyof typeof presets>) {
    const r = presets[key];
    if (r.start === range.start && r.end === range.end) {
      return PRESET_LABELS[key];
    }
  }
  if (range.start && range.end) {
    return `${shortDate(range.start)} – ${shortDate(range.end)}`;
  }
  if (range.start) return `From ${shortDate(range.start)}`;
  return `Until ${shortDate(range.end as string)}`;
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

// "MMM D" from an ISO YYYY-MM-DD string. Parsed off the literal parts to
// avoid a UTC-shift on ``new Date("YYYY-MM-DD")`` in negative offsets.
function shortDate(iso: string): string {
  const [y, m, d] = iso.split("-").map((p) => Number(p));
  if (!y || !m || !d) return iso;
  return `${MONTHS[m - 1]} ${d}`;
}
