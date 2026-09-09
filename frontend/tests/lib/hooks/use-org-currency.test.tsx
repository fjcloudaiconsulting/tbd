/**
 * `OrgCurrencyProvider` / `useOrgCurrency` / `useMoney` (TBD-503).
 *
 * ⚠⚠ THIS FILE IS MANDATORY, AND WHY IS THE POINT.
 * Before it, `grep -rln "OrgCurrencyProvider|useOrgCurrency|useMoney" tests/`
 * returned NOTHING. The provider — the whole mechanism this ticket ships — was
 * fenced only incidentally, by two accounts suites that happen to render a real
 * `AppShell`. `tests/lib/money.test.ts` covers the pure functions and none of
 * the wiring.
 *
 * That matters beyond tidiness: an architect round proposed globally stubbing
 * this provider in `vitest.setup.ts`. Stubbing a component with zero direct
 * coverage is the "a check that can only pass by deleting" shape — the stub
 * would have been indistinguishable from the feature not working.
 *
 * ⚠ The last two cases below are the ones a naive test omits, and both are
 * load-bearing:
 *   - `enabled={false}` must issue NO request. This is the auth gate, the same
 *     property `tests/app/accounts-swr-auth-gate.test.tsx` fences one layer
 *     down, and it is why `useMoney` must never fetch on its own.
 *   - A non-array payload must not throw. `deriveOrgCurrency`'s `Array.isArray`
 *     guard exists because a `?? []` nullish check let an error envelope reach
 *     a `for...of` and take down every money figure on the page (58 failures).
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import { apiFetch } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { OrgCurrencyProvider, useMoney, useOrgCurrency } from "@/lib/hooks/use-org-currency";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

function Probe() {
  const money = useMoney();
  const currency = useOrgCurrency();
  return (
    <>
      <span data-testid="figure">{money(1234.56)}</span>
      <span data-testid="currency">{currency ?? "<none>"}</span>
    </>
  );
}

/** Fresh SWR cache per test — the default cache is module-scoped and leaks. */
function renderIsolated(ui: React.ReactNode) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("with no provider", () => {
  it("returns undefined and renders bare, rather than throwing", async () => {
    // ⚠ The deliberate degrade. An earlier design called `useAuth()` here,
    // which THROWS outside a provider and broke 257 tests. Rendering bare is
    // both correct (no session, no currency) and identical to the
    // pre-TBD-503 output — never a wrong symbol.
    renderIsolated(<Probe />);
    // ⚠ THIS ASSERTION IS THE POINT. Without it the test passes against the
    // rejected design too: a `useOrgCurrency` that falls back to
    // `useAccounts()` with no provider renders IDENTICALLY here (unmocked
    // `apiFetch` resolves undefined -> `deriveOrgCurrency(undefined)` ->
    // undefined -> bare). The output cannot tell the two apart; only the
    // absence of the request can. That fallback is exactly what defeated
    // `accounts-swr-auth-gate.test.tsx`, so it is the defect worth killing.
    expect(apiFetch).not.toHaveBeenCalled();
    expect(screen.getByTestId("currency").textContent).toBe("<none>");
    expect(screen.getByTestId("figure").textContent).toBe(formatAmount(1234.56));
  });
});

describe("with a provider", () => {
  it("supplies the org currency and prefixes every figure", async () => {
    vi.mocked(apiFetch).mockResolvedValue([
      { currency: "EUR" },
      { currency: "EUR" },
    ] as never);

    renderIsolated(
      <OrgCurrencyProvider>
        <Probe />
      </OrgCurrencyProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("currency").textContent).toBe("EUR"),
    );
    expect(screen.getByTestId("figure").textContent).toBe(`€${formatAmount(1234.56)}`);
  });

  it("renders bare for a MIXED-currency org rather than guessing", async () => {
    // ⚠ The gate. No single symbol is correct when figures aggregate
    // differently-denominated accounts; labelling them with one would be a
    // wrong number wearing a confident label.
    vi.mocked(apiFetch).mockResolvedValue([
      { currency: "EUR" },
      { currency: "USD" },
    ] as never);

    renderIsolated(
      <OrgCurrencyProvider>
        <Probe />
      </OrgCurrencyProvider>,
    );

    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(screen.getByTestId("currency").textContent).toBe("<none>");
    expect(screen.getByTestId("figure").textContent).toBe(formatAmount(1234.56));
  });

  it("falls back to the code when the currency has no symbol", async () => {
    vi.mocked(apiFetch).mockResolvedValue([{ currency: "CHF" }] as never);
    renderIsolated(
      <OrgCurrencyProvider>
        <Probe />
      </OrgCurrencyProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("figure").textContent).toBe(`CHF ${formatAmount(1234.56)}`),
    );
  });
});

describe("⚠ the two properties a naive test omits", () => {
  it("issues NO request when disabled", async () => {
    // The auth gate. `AppShell` passes `enabled={!loading && !!user}`, and
    // `accounts-swr-auth-gate.test.tsx` fences the same property one layer
    // down. A provider that fetched regardless would defeat both.
    renderIsolated(
      <OrgCurrencyProvider enabled={false}>
        <Probe />
      </OrgCurrencyProvider>,
    );
    // ⚠ Two flushes inside `act`, matching `accounts-swr-auth-gate.test.tsx`.
    // A single microtask happens to be enough only because SWR calls the
    // fetcher synchronously from its mount layout effect when there is no
    // cached entry; its OTHER branch schedules through rAF. Relying on which
    // branch SWR takes would make this fence miss any provider that defers
    // its fetch by a frame or a timer.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiFetch).not.toHaveBeenCalled();
    expect(screen.getByTestId("figure").textContent).toBe(formatAmount(1234.56));
  });

  it("does not throw when the accounts payload is not an array", async () => {
    // An error envelope, a paginated object, a mocked `{}`. `?? []` does not
    // cover these — only null and undefined — so the value reached a
    // `for...of` and threw "is not iterable", blanking every money figure on
    // the page. This is read on every render of the authenticated tree.
    vi.mocked(apiFetch).mockResolvedValue({ detail: "boom" } as never);
    renderIsolated(
      <OrgCurrencyProvider>
        <Probe />
      </OrgCurrencyProvider>,
    );
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(screen.getByTestId("currency").textContent).toBe("<none>");
    expect(screen.getByTestId("figure").textContent).toBe(formatAmount(1234.56));
  });
});
