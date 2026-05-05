// Root-segment loading state. Renders while a route segment streams
// in or while async work in a Server Component is pending. Auth-
// neutral: shows a minimal centered spinner without depending on
// AppShell or session state.
export default function RootLoading() {
  return (
    <main
      role="status"
      aria-live="polite"
      aria-label="Loading"
      className="flex min-h-screen items-center justify-center bg-bg"
    >
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
    </main>
  );
}
