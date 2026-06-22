"use client";

import { useEffect, useState } from "react";

import { isApexBuild } from "@/lib/analytics";
import { BRAND_APP_URL } from "@/lib/links";

// Live founding-members counter. The count-only endpoint is public; on the
// apex static host it is fetched cross-origin from the app API, in the SSR
// app it is same-origin. Renders nothing until a real positive count
// arrives — we never show a baked or fake number, so on error/loading the
// offer line simply stands on its own.
export default function FounderCount() {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    if (typeof fetch !== "function") return;
    const base = isApexBuild ? BRAND_APP_URL : "";
    let alive = true;
    fetch(`${base}/api/v1/public/founder-count`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (alive && d && typeof d.count === "number") setCount(d.count);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  if (count === null || count <= 0) return null;
  return (
    <>
      {" "}
      <span aria-hidden className="text-text-muted/60">
        &middot;
      </span>{" "}
      <span>{count.toLocaleString()} founding members so far</span>
    </>
  );
}
