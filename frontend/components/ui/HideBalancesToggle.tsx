"use client";

import { Eye, EyeOff } from "lucide-react";

import { setBalancesHidden } from "@/lib/format";
import { useBalancesHidden } from "@/lib/hooks/use-org-currency";

// ⚠ The name never changes; state rides aria-pressed. Do not copy
// ThemeToggle's flipping label onto a toggle button.
export default function HideBalancesToggle() {
  const hidden = useBalancesHidden();
  const Icon = hidden ? EyeOff : Eye;
  return (
    <button
      type="button"
      onClick={() => setBalancesHidden(!hidden)}
      aria-label="Hide balances"
      aria-pressed={hidden}
      title="Hide balances"
      className="rounded-md p-2 text-text-muted transition-colors hover:text-text-primary"
    >
      <Icon aria-hidden="true" className="h-5 w-5" strokeWidth={1.5} />
    </button>
  );
}
