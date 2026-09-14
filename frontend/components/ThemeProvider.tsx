"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

type Theme = "light" | "dark";

interface ThemeContextValue {
  theme: Theme;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: "light",
  toggle: () => {},
});

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // Default is light (TBD-429). Only a stored "dark" selects dark; anything
  // else, including garbage, is light. Must agree with the pre-paint script in
  // app/layout.tsx, which sets the attribute before this effect runs.
  const [theme, setTheme] = useState<Theme>("light");

  useEffect(() => {
    // Storage can throw (Safari with site data blocked: SecurityError). An
    // unguarded throw here unmounts the root layout; fall back to light.
    let stored: string | null = null;
    try {
      stored = localStorage.getItem("tbd-theme");
    } catch {}
    if (stored === "dark") {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- restore persisted theme from localStorage after mount (client-only, avoids SSR hydration mismatch)
      setTheme("dark");
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", "light");
    }
  }, []);

  const toggle = useCallback(() => {
    const next = theme === "light" ? "dark" : "light";
    setTheme(next);
    if (next === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    try {
      localStorage.setItem("tbd-theme", next);
    } catch {
      // Not persisted; the theme still switches for this session.
    }
  }, [theme]);

  return (
    <ThemeContext.Provider value={{ theme, toggle }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}
