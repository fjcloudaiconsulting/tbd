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
  "ai.agent": {
    label: "AI Agent",
    description: "In-app assistant and MCP access to the agent tools over the org's own data.",
  },
};

// Mirrors FEATURE_MODULES / METER_MODULES in backend/app/auth/feature_catalog.py.
// A module groups the feature keys and usage meters sold together; every key and
// meter sits in exactly one. Pinned against the generated fixture by
// tests/lib/feature-catalog.test.ts.
export const FEATURE_MODULES: Record<string, readonly FeatureKey[]> = {
  ai: ["ai.agent", "ai.autocategorize", "ai.budget", "ai.forecast", "ai.smart_plan"],
};

export const METER_MODULES: Record<string, string> = {
  "assistant.turns": "ai",
  "mcp.calls": "ai",
  "platform_ai.tokens": "ai",
  "platform_ai.cents": "ai",
};
