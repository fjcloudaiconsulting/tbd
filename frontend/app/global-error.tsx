"use client";

import { useEffect } from "react";

// True global error boundary. Renders when an error escapes app/error.tsx,
// which only catches errors thrown below app/layout.tsx. Errors from inside
// the root layout itself (ThemeProvider, AuthProvider, the inline theme
// script, font loaders) bubble past app/error.tsx and end up here.
//
// Per Next.js docs, this file MUST own its own <html> and <body> because it
// fully replaces the root layout when it activates. It MUST NOT import any
// providers, hooks, or @/lib helpers that depend on layout-level setup —
// those are exactly the things that can be on fire when this page renders.
//
// Inline styles only (no Tailwind class evaluation, no globals.css) so a
// CSS pipeline failure doesn't take this page down with it.
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    if (process.env.NODE_ENV !== "production") {
      // eslint-disable-next-line no-console
      console.error("[global-error.tsx] caught:", error);
    }
  }, [error]);

  const wrapStyle: React.CSSProperties = {
    minHeight: "100vh",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: "1rem",
    background: "#0a0a0a",
    color: "#fafafa",
    fontFamily:
      "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  };
  const cardStyle: React.CSSProperties = {
    maxWidth: "28rem",
    width: "100%",
    padding: "1.5rem",
    border: "1px solid #2a2a2a",
    borderRadius: "0.5rem",
    background: "#171717",
  };
  const headingStyle: React.CSSProperties = {
    fontSize: "1.125rem",
    fontWeight: 600,
    color: "#f87171",
    margin: 0,
  };
  const bodyStyle: React.CSSProperties = {
    marginTop: "0.75rem",
    fontSize: "0.875rem",
    lineHeight: 1.6,
    color: "#a3a3a3",
  };
  const refStyle: React.CSSProperties = {
    marginTop: "0.75rem",
    fontSize: "0.75rem",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    color: "#737373",
  };
  const buttonRowStyle: React.CSSProperties = {
    marginTop: "1.25rem",
    display: "flex",
    flexDirection: "column",
    gap: "0.5rem",
  };
  const buttonStyle: React.CSSProperties = {
    minHeight: "44px",
    padding: "0.5rem 1rem",
    border: "1px solid #2a2a2a",
    borderRadius: "0.375rem",
    fontSize: "0.875rem",
    fontWeight: 500,
    background: "#262626",
    color: "#fafafa",
    cursor: "pointer",
    textDecoration: "none",
    textAlign: "center",
  };

  return (
    <html lang="en">
      <body style={wrapStyle}>
        <main role="alert" style={cardStyle}>
          <h1 style={headingStyle}>Something went wrong</h1>
          <p style={bodyStyle}>
            The application couldn&rsquo;t start. This is a low-level failure that bypassed the page-level error handler. You can reload, or head back to the home page.
          </p>
          {error?.digest && (
            <p style={refStyle}>
              Reference: <code>{error.digest}</code>
            </p>
          )}
          <div style={buttonRowStyle}>
            <button type="button" onClick={() => reset()} style={buttonStyle}>
              Reload application
            </button>
            <a href="/" style={buttonStyle}>
              Go to home page
            </a>
          </div>
        </main>
      </body>
    </html>
  );
}
