"use client";

import { useEffect, useState } from "react";

// Client island that renders the current year without freezing at build
// time on statically generated routes. The server emits the build-time
// year so crawlers see a reasonable value; the client re-evaluates on
// hydration, so a visitor landing on January 1 still sees the new year.
export default function CurrentYear() {
  const [year, setYear] = useState(() => new Date().getFullYear());

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- re-evaluate the current year on the client after hydration so a visitor landing on Jan 1 sees the new year (server emits the build-time year)
    setYear(new Date().getFullYear());
  }, []);

  return <span suppressHydrationWarning>{year}</span>;
}
