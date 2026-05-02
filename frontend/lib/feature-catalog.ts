import type { FeatureKey } from "@/lib/types";

export const FEATURE_LABELS: Record<FeatureKey, { label: string; description: string }> = {
  "ai.budget": {
    label: "AI Budget Rebalancing",
    description: "Suggests budget adjustments from spending patterns and one-time events.",
  },
  "ai.forecast": {
    label: "AI Smart Forecast",
    description: "Seasonality-aware forecast on top of the deterministic projection.",
  },
  "ai.smart_plan": {
    label: "AI Goal-Based Plans",
    description: "Generates a savings + budget plan to hit a stated goal by a target date.",
  },
  "ai.autocategorize": {
    label: "AI Auto-Categorization",
    description: "LLM fallback for transactions the deterministic rules can't categorize.",
  },
};
