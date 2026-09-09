/**
 * The provider's ALTITUDE (TBD-503).
 *
 * ⚠⚠ THIS IS THE ONE REGRESSION NOBODY ELSE CAN SEE.
 * `OrgCurrencyProvider` must sit ABOVE every page component. It briefly sat
 * inside `AppShell` instead, and the whole suite stayed green: no RTL test
 * renders the root layout, so `useMoney()` returns undefined in all of them
 * and every money figure renders byte-identically either way. The operator
 * found it by hand — account tiles showed "€16,062.60" while the transactions,
 * budgets and forecast Amount columns stayed bare.
 *
 * The cause is structural, not a wiring slip: pages here are shaped
 *
 *   function XPage() { const money = useMoney(); … return <AppShell>…</AppShell> }
 *
 * so a provider mounted inside `AppShell` is created BELOW the component that
 * formats the money. A component cannot consume a provider it renders.
 *
 * ⚠ This walks the REAL element tree returned by `RootLayout`, rather than
 * grepping `layout.tsx` for the component name. A grep is satisfied by a
 * mention in a comment, by an import that is never used, and by a boundary
 * rendered as a SIBLING of `children` rather than an ancestor — all three of
 * which are exactly the broken states. Ancestry is the property; only the tree
 * carries it.
 */
import { isValidElement, type ReactElement, type ReactNode } from "react";

import RootLayout from "@/app/layout";
import OrgCurrencyBoundary from "@/components/OrgCurrencyBoundary";

// The layout is a Server Component that awaits the per-request CSP nonce via
// `next/headers`, which does not exist in jsdom. Nothing here depends on its
// value.
vi.mock("@/lib/nonce", () => ({ readNonce: async () => "" }));

const SENTINEL = <div data-testid="the-page" />;

/** Every ancestor chain from the root down to `target`, or null if absent. */
function pathTo(node: ReactNode, target: ReactNode): ReactElement[] | null {
  if (node === target) return [];
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = pathTo(child, target);
      if (found) return found;
    }
    return null;
  }
  if (!isValidElement(node)) return null;
  const el = node as ReactElement<{ children?: ReactNode }>;
  const found = pathTo(el.props?.children, target);
  return found ? [el, ...found] : null;
}

describe("root layout — currency provider altitude", () => {
  it("mounts OrgCurrencyBoundary as an ANCESTOR of the page, not a sibling", async () => {
    const tree = await RootLayout({ children: SENTINEL });

    const path = pathTo(tree, SENTINEL);
    expect(path, "the layout must render its `children`").not.toBeNull();

    const types = path!.map((el) => el.type);
    expect(
      types,
      "OrgCurrencyBoundary must wrap the page, or every money figure formatted " +
        "in a page BODY renders bare while its children render prefixed",
    ).toContain(OrgCurrencyBoundary);
  });

  it("keeps the boundary INSIDE AuthProvider, since it reads the session", async () => {
    // `OrgCurrencyBoundary` calls `useAuth()` to gate the accounts fetch on a
    // resolved session. `useAuth` throws outside its provider, so the ordering
    // here is load-bearing in the other direction too.
    const { AuthProvider } = await import("@/components/auth/AuthProvider");
    const tree = await RootLayout({ children: SENTINEL });
    const types = pathTo(tree, SENTINEL)!.map((el) => el.type);

    expect(types).toContain(AuthProvider);
    expect(types.indexOf(AuthProvider)).toBeLessThan(
      types.indexOf(OrgCurrencyBoundary),
    );
  });
});
