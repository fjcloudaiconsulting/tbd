"use client";

/**
 * Labelled section wrapper shared by every widget-editor control block
 * (extracted verbatim from the original widget config rail). Renders an uppercase label
 * with an optional ``HelpTooltip`` info icon, then the control children.
 */
import HelpTooltip from "@/components/help/HelpTooltip";
import type { HelpTooltipKey } from "@/lib/help/tooltips";

export default function Section({
  label,
  help,
  children,
}: {
  label: string;
  /** Optional help-tooltip key rendered as an info icon next to the label. */
  help?: HelpTooltipKey;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1 text-[11px] font-medium uppercase tracking-wider text-text-muted">
        <span>{label}</span>
        {help && <HelpTooltip k={help} />}
      </div>
      {children}
    </div>
  );
}
