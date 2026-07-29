/**
 * Wire types and marker vocabulary for `/settings/organization/periods`.
 *
 * Spec: `specs/2026-07-29-billing-period-roster-design.md` §1.1, §2.1, §2.5.
 * The backend contract this mirrors is `backend/app/schemas/billing_roster.py`.
 *
 * This module is deliberately local to the page rather than living in
 * `lib/styles.ts`: §1.1 allows exactly ONE new style primitive (`warning`),
 * and a kind -> severity lookup is page vocabulary, not a design token.
 *
 * Two rules here are normative and both have a fence in the test file:
 *
 * * **`ROSTER_SCOPED` is an explicit set, never a truthiness check.**
 *   `off_window` is vacuously `false` on `no_open`,
 *   `overlap_analysis_skipped` and `overlap_emission_capped` (they name no
 *   in-window id, or no id at all), so a band written as
 *   `anomalies.filter((a) => a.off_window)` erases `no_open` on the exact
 *   org this page exists for: 400 periods, all lapsed, none open.
 * * **Unknown kinds are rendered, never dropped.** §2.5 requires clients to
 *   tolerate a kind they do not know; `describeAnomaly` returns a neutral
 *   marker carrying the raw string rather than throwing or skipping.
 */

export type PeriodStatus =
  | "open"
  | "upcoming"
  | "current_by_calendar"
  | "past"
  | "invalid";

export interface RosterScope {
  period_count: number;
  first_start: string | null;
  last_start: string | null;
  analyzed: boolean;
}

export interface WindowScope {
  from: string | null;
  /** Permanently null (§2.5): the display window has no upper bound. */
  to: string | null;
  displayed_count: number;
  truncated: boolean;
}

export interface RosterPeriod {
  id: number;
  start_date: string;
  end_date: string | null;
  /** `period_effective_end` semantics: derived, no clock floor. */
  effective_end: string | null;
  /** `period_spend_window_end` semantics: floored at today on an open row. */
  counting_through: string | null;
  status: PeriodStatus;
  length_days: number | null;
  transaction_count: number;
  /** Decimal-as-string, per the repo's wire convention. */
  settled_net: string;
}

export interface ReferencedPeriod {
  id: number;
  start_date: string;
  end_date: string | null;
  effective_end: string | null;
  status: PeriodStatus;
}

interface AnomalyCommon {
  /**
   * True when any id the marker names is absent from `periods`. ⚠ Vacuously
   * false on every roster-scoped kind; never use it to decide whether one of
   * those renders.
   */
  off_window: boolean;
}

export interface GapAnomaly extends AnomalyCommon {
  kind: "gap";
  from_period_id: number;
  to_period_id: number;
  from_date: string;
  to_date: string;
}

export interface OverlapAnomaly extends AnomalyCommon {
  kind: "overlap";
  from_period_id: number;
  to_period_id: number;
  from_date: string;
  to_date: string;
}

export interface DuplicateOpenAnomaly extends AnomalyCommon {
  kind: "duplicate_open";
  period_ids: number[];
}

export interface NoOpenAnomaly extends AnomalyCommon {
  kind: "no_open";
  period_ids: number[];
}

export interface InvertedAnomaly extends AnomalyCommon {
  kind: "inverted";
  period_id: number;
}

export interface StraddlingAnomaly extends AnomalyCommon {
  kind: "straddling";
  period_id: number;
  anchor_period_id: number;
}

export interface LapsedOpenAnomaly extends AnomalyCommon {
  kind: "lapsed_open";
  period_id: number;
  effective_end: string;
}

export interface OverlapAnalysisSkippedAnomaly extends AnomalyCommon {
  kind: "overlap_analysis_skipped";
  period_count: number;
  cap: number;
}

export interface OverlapEmissionCappedAnomaly extends AnomalyCommon {
  kind: "overlap_emission_capped";
  overlap_count: number;
  cap: number;
}

export type KnownAnomaly =
  | GapAnomaly
  | OverlapAnomaly
  | DuplicateOpenAnomaly
  | NoOpenAnomaly
  | InvertedAnomaly
  | StraddlingAnomaly
  | LapsedOpenAnomaly
  | OverlapAnalysisSkippedAnomaly
  | OverlapEmissionCappedAnomaly;

/** A kind this build does not know. Rendered neutral, never dropped. */
export interface UnknownAnomaly extends AnomalyCommon {
  kind: string;
}

export type RosterAnomaly = KnownAnomaly | UnknownAnomaly;

export interface RosterResponse {
  roster: RosterScope;
  window: WindowScope;
  periods: RosterPeriod[];
  anomalies: RosterAnomaly[];
  referenced_periods: Record<string, ReferencedPeriod>;
}

const KNOWN_KINDS = [
  "gap",
  "overlap",
  "duplicate_open",
  "no_open",
  "inverted",
  "straddling",
  "lapsed_open",
  "overlap_analysis_skipped",
  "overlap_emission_capped",
] as const;

export type KnownKind = (typeof KNOWN_KINDS)[number];

const KNOWN_KIND_SET: ReadonlySet<string> = new Set<string>(KNOWN_KINDS);

/**
 * ⚠ The three kinds that describe the ROSTER rather than a row. They have no
 * row to sit on and no id to be off-window, so the summary band renders them
 * unconditionally. Never derived from `off_window`.
 */
export const ROSTER_SCOPED: ReadonlySet<string> = new Set<string>([
  "no_open",
  "overlap_analysis_skipped",
  "overlap_emission_capped",
]);

function isKnownAnomaly(anomaly: RosterAnomaly): anomaly is KnownAnomaly {
  return KNOWN_KIND_SET.has(anomaly.kind);
}

/** Severity tiers. `badgeSuccess` is excluded: no marker is good news. */
export type Tier = "error" | "warning" | "info" | "neutral";

const TIER_BY_KIND: Record<KnownKind, Tier> = {
  inverted: "error",
  overlap: "error",
  duplicate_open: "error",
  gap: "warning",
  lapsed_open: "warning",
  no_open: "warning",
  // ⚠ WARNING, not neutral: the overlap check did not run, which is the one
  // condition that falsifies the guarantee sentence.
  overlap_analysis_skipped: "warning",
  straddling: "info",
  // ⚠ NEUTRAL: detection was complete, only the listing is truncated.
  overlap_emission_capped: "neutral",
};

/** Rank used to pick the page verdict. Higher wins. */
const TIER_RANK: Record<Tier, number> = {
  neutral: 0,
  info: 1,
  warning: 2,
  error: 3,
};

export function highestTier(anomalies: RosterAnomaly[], refs: Record<string, ReferencedPeriod>): Tier | null {
  let worst: Tier | null = null;
  for (const anomaly of anomalies) {
    const tier = describeAnomaly(anomaly, refs).tier;
    if (worst === null || TIER_RANK[tier] > TIER_RANK[worst]) worst = tier;
  }
  return worst;
}

export const STATUS_WORD: Record<PeriodStatus, string> = {
  open: "Open",
  upcoming: "Upcoming",
  current_by_calendar: "Current by calendar",
  past: "Past",
  invalid: "Invalid",
};

export function statusWord(status: PeriodStatus | string): string {
  return STATUS_WORD[status as PeriodStatus] ?? status;
}

function isoToUTC(iso: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return NaN;
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

/** Inclusive day span between two ISO dates. Parsed part-wise, so no TZ shift. */
export function inclusiveDays(fromISO: string, toISO: string): number {
  const a = isoToUTC(fromISO);
  const b = isoToUTC(toISO);
  if (Number.isNaN(a) || Number.isNaN(b)) return 0;
  return Math.round((b - a) / 86400000) + 1;
}

function startOf(refs: Record<string, ReferencedPeriod>, id: number): string {
  return refs[String(id)]?.start_date ?? `period ${id}`;
}

export interface MarkerCopy {
  tier: Tier;
  /** Never abbreviated: colour carries severity, the label carries the kind. */
  label: string;
  explanation: string;
}

/**
 * One marker -> the tier, the label and the sentence that explains it.
 *
 * Every branch returns an explanation. Colour alone is never the signal, so a
 * monochrome screenshot loses nothing.
 */
export function describeAnomaly(
  anomaly: RosterAnomaly,
  refs: Record<string, ReferencedPeriod>,
): MarkerCopy {
  if (!isKnownAnomaly(anomaly)) {
    return {
      tier: "neutral",
      label: anomaly.kind,
      explanation:
        "This organization reported an issue this page does not recognize yet. Nothing is missing from the checks; only the wording is.",
    };
  }
  const tier = TIER_BY_KIND[anomaly.kind];
  switch (anomaly.kind) {
    case "gap":
      return {
        tier,
        label: "Coverage gap",
        explanation: `Nothing covers ${anomaly.from_date} to ${anomaly.to_date}. Transactions settling in those ${inclusiveDays(anomaly.from_date, anomaly.to_date)} days belong to no period.`,
      };
    case "overlap":
      return {
        tier,
        label: "Overlapping periods",
        explanation: `The period starting ${startOf(refs, anomaly.from_period_id)} and the period starting ${startOf(refs, anomaly.to_period_id)} both cover ${anomaly.from_date} to ${anomaly.to_date}. Transactions in that range are counted twice.`,
      };
    case "duplicate_open":
      return {
        tier,
        label: "More than one open period",
        explanation: `${anomaly.period_ids.length} periods are open at once. Different screens can pick different ones as the current period.`,
      };
    case "no_open":
      return {
        tier,
        label: "No open period",
        explanation:
          "This organization has no period with an open end. The next screen that asks for the current period will create one.",
      };
    case "inverted":
      return {
        tier,
        label: "End before start",
        explanation: "This period ends before it starts, so it covers nothing.",
      };
    case "straddling":
      return {
        tier,
        label: "Straddles the open period",
        explanation: `This period runs into the open period starting ${startOf(refs, anomaly.anchor_period_id)}. Closing the open period will not clear it.`,
      };
    case "lapsed_open":
      return {
        tier,
        label: "Open period has lapsed",
        explanation: `This period's derived end, ${anomaly.effective_end}, is in the past.`,
      };
    case "overlap_analysis_skipped":
      return {
        tier,
        label: "Overlap check skipped",
        explanation: `This roster has ${anomaly.period_count} periods, over the ${anomaly.cap} limit for the overlap check. Gap and open-period checks still ran.`,
      };
    case "overlap_emission_capped":
      return {
        tier,
        label: "Overlap list truncated",
        explanation: `${anomaly.overlap_count} overlapping pairs were found. The first ${anomaly.cap} are reported.`,
      };
  }
}

/** Every period id a marker names. Empty for roster-scoped and unknown kinds. */
export function anomalyPeriodIds(anomaly: RosterAnomaly): number[] {
  if (!isKnownAnomaly(anomaly)) return [];
  switch (anomaly.kind) {
    case "gap":
    case "overlap":
      return [anomaly.from_period_id, anomaly.to_period_id];
    case "duplicate_open":
    case "no_open":
      return anomaly.period_ids;
    case "inverted":
    case "lapsed_open":
      return [anomaly.period_id];
    case "straddling":
      return [anomaly.period_id, anomaly.anchor_period_id];
    default:
      return [];
  }
}

/**
 * The summary band's contents. ⚠ The `ROSTER_SCOPED` union is the point: a
 * plain `off_window` filter erases `no_open` and both refusal markers.
 */
export function bandAnomalies(anomalies: RosterAnomaly[]): RosterAnomaly[] {
  return anomalies.filter(
    (anomaly) => ROSTER_SCOPED.has(anomaly.kind) || anomaly.off_window,
  );
}

/**
 * Markers that render inline on a displayed row.
 *
 * `gap` is excluded on purpose: §1.1 renders it as a BREAK IN THE RAIL, an
 * interstitial between the two rows it names, never as a chip on either.
 * Roster-scoped kinds are excluded because they name no row.
 */
export function inlineAnomaliesFor(
  anomalies: RosterAnomaly[],
  periodId: number,
): RosterAnomaly[] {
  return anomalies.filter(
    (anomaly) =>
      anomaly.kind !== "gap" &&
      !ROSTER_SCOPED.has(anomaly.kind) &&
      anomalyPeriodIds(anomaly).includes(periodId),
  );
}

/**
 * Gaps expressible as geometry: both named rows are on screen, so the rail can
 * visibly stop and restart between them. Off-window gaps go to the band.
 */
export function railBreakGaps(anomalies: RosterAnomaly[]): GapAnomaly[] {
  return anomalies.filter(
    (anomaly): anomaly is GapAnomaly =>
      anomaly.kind === "gap" && !anomaly.off_window,
  );
}
