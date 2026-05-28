"use client";

/**
 * HelpTooltip — content-map-driven wrapper around the base Tooltip
 * primitive (L5.3 — help residuals).
 *
 * Use this for every frequently confused field. Define the copy once
 * in ``frontend/lib/help/tooltips.ts`` keyed by a stable id, then drop
 * ``<HelpTooltip k="tx.frequency" />`` next to the matching label. The
 * underlying ``<Tooltip>`` owns the portal, accessibility wiring
 * (aria-describedby), reduced-motion, and viewport clamping.
 *
 * The single-letter ``k`` prop name keeps call sites tight when many
 * tooltips share a row, e.g.
 *
 *   <label>Amount <HelpTooltip k="tx.amount" /></label>
 *
 * Callers that need to override the trigger label can pass it
 * directly; everything else comes from the content map.
 */
import { useId } from "react";

import Tooltip from "@/components/Tooltip";
import {
  getHelpTooltip,
  type HelpTooltipKey,
} from "@/lib/help/tooltips";

export interface HelpTooltipProps {
  /** Content-map key. See ``HELP_TOOLTIPS`` in ``lib/help/tooltips.ts``. */
  k: HelpTooltipKey;
  /** Override the trigger ARIA label (rarely needed). */
  triggerLabel?: string;
  /** Optional class overrides forwarded to the default trigger. */
  className?: string;
}

export default function HelpTooltip({
  k,
  triggerLabel,
  className,
}: HelpTooltipProps) {
  const entry = getHelpTooltip(k);
  // Per-instance suffix so the same key can be rendered multiple
  // times on the same page (e.g. the inline transactions edit row
  // AND the floating quick-add form both render `tx.amount`) without
  // generating duplicate DOM ids — which would break aria-describedby.
  // The stable key prefix keeps the id greppable in dev tools.
  const instanceId = useId().replace(/:/g, "");
  return (
    <Tooltip
      content={entry.content}
      learnMoreSection={entry.learnMoreSection}
      triggerLabel={triggerLabel ?? entry.triggerLabel ?? "More info"}
      className={className}
      id={`help-${k.replace(/\./g, "-")}-${instanceId}`}
    />
  );
}
