"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

/**
 * Read a single-use token (reset, verify, invite, MFA) from the query string
 * once, then drop it from the address bar so it does not stay in browser
 * history or in the Referer of later requests (FE-03).
 *
 * The value is captured in state on the first render. Next.js syncs
 * ``useSearchParams`` with ``history.replaceState``, so reading the param on
 * every render would lose the token as soon as it is stripped. The strip runs
 * in an effect declared before the caller's own effects, so a request the page
 * fires on mount already goes out from the clean URL.
 */
export function useOneTimeQueryParam(name: string): string | null {
  const searchParams = useSearchParams();
  const [value] = useState(() => searchParams.get(name));

  useEffect(() => {
    const url = new URL(window.location.href);
    if (!url.searchParams.has(name)) return;
    url.searchParams.delete(name);
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }, [name]);

  return value;
}
