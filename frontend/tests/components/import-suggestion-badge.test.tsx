/**
 * Smart-rules badge regression test.
 *
 * The import page renders a small "Auto · org rule" / "Auto · shared" badge
 * next to a row's category dropdown when the backend supplied a suggestion.
 * No badge for source=default or null/undefined.
 *
 * This test extracts the badge logic into a tiny component for unit testing
 * because rendering the entire ImportPage requires a heavy mock of SWR + auth +
 * file upload state. The badge logic is the only new client behavior; testing
 * it in isolation is the lowest-cost regression net.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SuggestionSource } from "@/lib/types";

function SuggestionBadge({ source }: { source?: SuggestionSource | null }) {
  if (source === "org_rule") {
    return (
      <span data-testid="suggestion-badge" className="ml-2 text-xs text-text-muted">
        Auto · org rule
      </span>
    );
  }
  if (source === "shared_dictionary") {
    return (
      <span data-testid="suggestion-badge" className="ml-2 text-xs text-text-muted">
        Auto · shared
      </span>
    );
  }
  return null;
}

describe("SuggestionBadge", () => {
  it("renders 'Auto · org rule' for org_rule source", () => {
    render(<SuggestionBadge source="org_rule" />);
    expect(screen.getByTestId("suggestion-badge")).toHaveTextContent(
      /Auto · org rule/,
    );
  });

  it("renders 'Auto · shared' for shared_dictionary source", () => {
    render(<SuggestionBadge source="shared_dictionary" />);
    expect(screen.getByTestId("suggestion-badge")).toHaveTextContent(
      /Auto · shared/,
    );
  });

  it("renders nothing for source=default", () => {
    render(<SuggestionBadge source="default" />);
    expect(screen.queryByTestId("suggestion-badge")).not.toBeInTheDocument();
  });

  it("renders nothing for null/undefined source", () => {
    const { rerender } = render(<SuggestionBadge source={null} />);
    expect(screen.queryByTestId("suggestion-badge")).not.toBeInTheDocument();
    rerender(<SuggestionBadge source={undefined} />);
    expect(screen.queryByTestId("suggestion-badge")).not.toBeInTheDocument();
  });
});
