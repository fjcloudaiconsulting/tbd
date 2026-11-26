import { useEffect, useState } from "react";

const MOBILE_QUERY = "(max-width: 639px)";

/**
 * Returns whether ``query`` currently matches. SSR-safe — returns false
 * until mounted (and wherever matchMedia is missing) — and subscribes to
 * ``change`` events so rotating a phone or resizing a window updates the
 * value reactively.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mq = window.matchMedia(query);
    const update = () => setMatches(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [query]);
  return matches;
}

/**
 * Returns true when the viewport is below Tailwind's ``sm`` breakpoint
 * (≤639px).
 */
export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
