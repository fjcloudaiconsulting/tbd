/**
 * The pre-paint theme bootstrap in the root layout (TBD-429).
 *
 * The light theme is the default. `ThemeProvider` restores the stored theme in
 * an effect, which runs AFTER first paint, so the attribute a visitor first
 * sees is decided by the inline <script> in `app/layout.tsx`. That script is a
 * string, so a grep over it proves nothing about what it does: this walks the
 * element tree `RootLayout` actually returns, pulls the script's `__html`, and
 * EXECUTES it against a stubbed `localStorage` and `document`.
 */
import { isValidElement, type ReactElement, type ReactNode } from "react";

import RootLayout from "@/app/layout";

vi.mock("@/lib/nonce", () => ({ readNonce: async () => "" }));

function findScripts(node: ReactNode, out: string[] = []): string[] {
  if (Array.isArray(node)) {
    node.forEach((child) => findScripts(child, out));
    return out;
  }
  if (!isValidElement(node)) return out;
  const el = node as ReactElement<{
    children?: ReactNode;
    dangerouslySetInnerHTML?: { __html: string };
  }>;
  if (el.type === "script" && el.props.dangerouslySetInnerHTML) {
    out.push(el.props.dangerouslySetInnerHTML.__html);
  }
  findScripts(el.props?.children, out);
  return out;
}

async function bootstrapScript(): Promise<string> {
  const tree = await RootLayout({ children: <div /> });
  const scripts = findScripts(tree).filter((s) => s.includes("tbd-theme"));
  expect(scripts, "exactly one inline theme bootstrap script").toHaveLength(1);
  return scripts[0];
}

/** Run the script; return the data-theme it leaves on the root, or null. */
function runWith(script: string, getItem: () => string | null): string | null {
  const attrs = new Map<string, string>();
  const documentStub = {
    documentElement: {
      setAttribute: (k: string, v: string) => attrs.set(k, v),
      removeAttribute: (k: string) => attrs.delete(k),
    },
  };
  const localStorageStub = { getItem: vi.fn(getItem) };
  new Function("localStorage", "document", script)(
    localStorageStub,
    documentStub,
  );
  expect(localStorageStub.getItem).toHaveBeenCalledWith("tbd-theme");
  return attrs.get("data-theme") ?? null;
}

describe("root layout theme bootstrap script", () => {
  it("paints light when nothing is stored", async () => {
    expect(runWith(await bootstrapScript(), () => null)).toBe("light");
  });

  it("paints dark (no attribute) when 'dark' is stored", async () => {
    expect(runWith(await bootstrapScript(), () => "dark")).toBeNull();
  });

  it("paints light when 'light' is stored", async () => {
    expect(runWith(await bootstrapScript(), () => "light")).toBe("light");
  });

  it("paints light for a garbage stored value", async () => {
    expect(runWith(await bootstrapScript(), () => "Dark ")).toBe("light");
  });

  it("paints light, without throwing, when storage access throws", async () => {
    const script = await bootstrapScript();
    expect(
      runWith(script, () => {
        throw new Error("SecurityError");
      }),
    ).toBe("light");
  });
});
