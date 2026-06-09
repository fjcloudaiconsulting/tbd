// frontend/components/landing/ComparisonTable.tsx
// Accessible comparison table. Token-only colors (no raw palette; CI-checked).
// Cells carry a short factual phrase AND a non-visual support label, so meaning
// never rides on color or glyph alone (WCAG 2.2 AA). Server-rendered, no client JS.
import {
  type Competitor,
  capabilityDimensions,
  comparisonMatrix,
  competitorMeta,
  dimensionLabels,
  dimensionOrder,
} from "@/lib/comparison";

const supportLabel: Record<"yes" | "no" | "partial", string> = {
  yes: "Yes",
  no: "No",
  partial: "Partial",
};

function SupportGlyph({ supported }: { supported: "yes" | "no" | "partial" }) {
  const path =
    supported === "yes"
      ? "M3.5 8.5l3 3 6-7"
      : supported === "no"
        ? "M4 8h8"
        : "M3.5 9.5c2-3 5 3 9 0";
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="h-4 w-4 flex-shrink-0">
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function ComparisonTable({
  competitors,
}: {
  competitors: ReadonlyArray<Competitor>;
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-border bg-surface">
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-border">
            <th scope="col" className="px-4 py-3 font-medium text-text-muted">
              How they compare
            </th>
            {competitors.map((c) => (
              <th
                key={c}
                scope="col"
                className="px-4 py-3 font-display font-semibold text-text-primary"
              >
                {competitorMeta[c].name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {dimensionOrder.map((dim) => (
            <tr key={dim} className="border-b border-border last:border-0">
              <th
                scope="row"
                className="px-4 py-3 align-top font-medium text-text-primary"
              >
                {dimensionLabels[dim]}
              </th>
              {competitors.map((c) => {
                const cell = comparisonMatrix[dim][c];
                const isCapability = capabilityDimensions.has(dim);
                return (
                  <td
                    key={c}
                    className="px-4 py-3 align-top text-text-secondary"
                  >
                    <span className="flex items-start gap-2">
                      {isCapability && (
                        <span className="mt-0.5 text-text-muted">
                          <SupportGlyph supported={cell.supported} />
                          <span className="sr-only">
                            {supportLabel[cell.supported]}
                          </span>
                        </span>
                      )}
                      <span>{cell.value}</span>
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
