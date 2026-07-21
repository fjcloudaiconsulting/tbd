import { fireEvent, render, screen } from "@testing-library/react";

import TokenList from "@/components/system/api-tokens/TokenList";
import type { ApiToken } from "@/lib/types";

// A fixed "now" so the expiry-tone math is deterministic regardless of the
// wall clock. TokenList takes `nowMs` as a prop for exactly this reason.
const NOW = Date.parse("2026-07-21T12:00:00Z");

function daysFromNow(days: number): string {
  return new Date(NOW + days * 24 * 60 * 60 * 1000).toISOString();
}

function makeToken(overrides: Partial<ApiToken> = {}): ApiToken {
  return {
    id: 1,
    name: "broadcast cron",
    prefix: "pat_a1b2c3",
    scope: "read",
    created_at: "2026-07-01T00:00:00Z",
    expires_at: daysFromNow(30),
    last_used_at: null,
    status: "active",
    ...overrides,
  };
}

describe("TokenList", () => {
  it("renders a row with name, prefix, scope, status and a Revoke button", () => {
    render(
      <TokenList tokens={[makeToken()]} nowMs={NOW} onRevoke={vi.fn()} />,
    );
    const row = screen.getByTestId("api-token-row-1");
    expect(row).toHaveTextContent("broadcast cron");
    expect(row).toHaveTextContent("pat_a1b2c3");
    expect(row).toHaveTextContent(/read-only/i);
    expect(row).toHaveTextContent(/active/i);
    expect(screen.getByTestId("api-token-revoke-1")).toBeInTheDocument();
  });

  it("shows a 'Read & write' label for a write-scoped token", () => {
    render(
      <TokenList
        tokens={[makeToken({ scope: "write" })]}
        nowMs={NOW}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByTestId("api-token-row-1")).toHaveTextContent(/read & write/i);
  });

  it("shows an amber tone when expiry is within 14 days", () => {
    render(
      <TokenList
        tokens={[makeToken({ id: 2, expires_at: daysFromNow(10) })]}
        nowMs={NOW}
        onRevoke={vi.fn()}
      />,
    );
    const cell = screen.getByTestId("api-token-expiry-2");
    expect(cell).toHaveAttribute("data-tone", "warning");
    expect(cell).toHaveTextContent(/in 10 days/i);
  });

  it("shows a red tone when expiry is within 3 days", () => {
    render(
      <TokenList
        tokens={[makeToken({ id: 3, expires_at: daysFromNow(2) })]}
        nowMs={NOW}
        onRevoke={vi.fn()}
      />,
    );
    const cell = screen.getByTestId("api-token-expiry-3");
    expect(cell).toHaveAttribute("data-tone", "danger");
    expect(cell).toHaveTextContent(/in 2 days/i);
  });

  it("renders an Expired badge for an expired token and a Revoked badge for a revoked one", () => {
    render(
      <TokenList
        tokens={[
          makeToken({ id: 4, status: "expired", expires_at: daysFromNow(-1) }),
          makeToken({ id: 5, status: "revoked" }),
        ]}
        nowMs={NOW}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByTestId("api-token-status-4")).toHaveTextContent(/expired/i);
    expect(screen.getByTestId("api-token-status-5")).toHaveTextContent(/revoked/i);
    // A revoked/expired token exposes no Revoke action.
    expect(screen.queryByTestId("api-token-revoke-4")).not.toBeInTheDocument();
    expect(screen.queryByTestId("api-token-revoke-5")).not.toBeInTheDocument();
  });

  it("shows 'Never' when last_used_at is null and the timestamp otherwise", () => {
    render(
      <TokenList
        tokens={[makeToken({ id: 6, last_used_at: "2026-07-20T09:30:00Z" })]}
        nowMs={NOW}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByTestId("api-token-lastused-6")).toHaveTextContent("2026-07-20");
    render(
      <TokenList
        tokens={[makeToken({ id: 7, last_used_at: null })]}
        nowMs={NOW}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByTestId("api-token-lastused-7")).toHaveTextContent(/never/i);
  });

  it("shows the empty state when there are no tokens", () => {
    render(<TokenList tokens={[]} nowMs={NOW} onRevoke={vi.fn()} />);
    expect(screen.getByTestId("api-token-empty")).toBeInTheDocument();
  });

  it("calls onRevoke with the token when Revoke is clicked", () => {
    const onRevoke = vi.fn();
    const token = makeToken();
    render(<TokenList tokens={[token]} nowMs={NOW} onRevoke={onRevoke} />);
    fireEvent.click(screen.getByTestId("api-token-revoke-1"));
    expect(onRevoke).toHaveBeenCalledWith(token);
  });
});
