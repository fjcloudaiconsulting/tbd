import { useEffect, useState } from "react";

const MOBILE_QUERY = "(max-width: 639px)";

/**
 * Returns true when the viewport is below Tailwind's ``sm`` breakpoint
 * (≤639px). SSR-safe — returns false until mounted — and subscribes to
 * ``change`` events so rotating a phone or resizing a window updates the
 * value reactively.
 */
export function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mq = window.matchMedia(MOBILE_QUERY);
    const update = () => setMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return mobile;
}
