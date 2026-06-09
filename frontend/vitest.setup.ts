import "@testing-library/jest-dom/vitest";

// jsdom's Blob/File do not implement the async ``Blob.text()`` reader that
// every real browser provides. The import page's file-format sniff calls
// ``await file.slice(0, 4096).text()`` to peek at an ambiguous upload, so
// without this polyfill that call throws in tests (and the sniff would
// silently fall back to CSV for every file). Back it with the FileReader
// jsdom *does* implement so sliced content round-trips correctly.
if (typeof Blob !== "undefined" && typeof Blob.prototype.text !== "function") {
  Blob.prototype.text = function text(this: Blob): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result ?? ""));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(this);
    });
  };
}

// Reset localStorage between tests so persisted sort/filter state from a
// previous test doesn't bleed into the next render. The persistence hooks
// (lib/hooks/use-persisted-sort, use-persisted-filters) read on mount, so
// without this isolation a test that exercises a non-default sort would
// alter the fixture for everything that follows.
beforeEach(() => {
  if (typeof window !== "undefined") {
    window.localStorage.clear();
  }
});

// Global stub for the announcement banner.
//
// ``AnnouncementBar`` is mounted unconditionally in ``AppShell`` and
// fetches ``/api/v1/announcements`` on every page render. Almost every
// page test mocks ``apiFetch`` with ad-hoc ``mockResolvedValueOnce``
// chains scoped to the page's own endpoints, so the bar's hidden call
// either (a) consumes a fixture meant for the page (causing the page
// to receive ``undefined`` from the now-empty queue) or (b) crashes
// the global render with ``items.map is not a function``.
//
// Tests that exercise the bar itself live in
// ``tests/components/announcements/announcement-bar.test.tsx`` and
// import the component directly — they call ``vi.unmock`` to restore
// the real module before their own ``vi.mock`` declaration.
//
// Architect-locked decision (PR #340 review, 2026-05-22): the global
// stub is at the *component* layer rather than the route table layer
// because there is no shared route-table helper today, and adding one
// would touch every page test. A no-op stub keeps the AppShell test
// surface honest without forcing each test to re-declare an
// announcement fixture.
vi.mock("@/components/announcements/AnnouncementBar", () => ({
  __esModule: true,
  default: () => null,
}));

// Global stub for the notification bell.
//
// Same reasoning as the AnnouncementBar stub above: ``NotificationBell``
// is mounted unconditionally in the ``AppShell`` header row and on
// every page render it fires ``GET /api/v1/notifications?limit=10``
// via SWR. That polls every 60s and refetches on focus, so without a
// global stub every page test must add a no-op handler for that URL
// to keep ad-hoc ``mockResolvedValueOnce`` queues honest.
//
// Tests that exercise the bell itself live in
// ``tests/components/notifications/notification-bell.test.tsx`` and
// import the component directly — they call ``vi.unmock`` to restore
// the real module before their own ``vi.mock`` declaration.
vi.mock("@/components/notifications/NotificationBell", () => ({
  __esModule: true,
  default: () => null,
}));

// Same reasoning: AI surfaces call ``useAiStatus`` which fires
// ``GET /api/v1/ai/status`` via SWR. Default it to ``undefined`` (status
// unresolved -> AI affordances stay hidden) so ad-hoc ``mockResolvedValueOnce``
// queues aren't consumed by the status call. Tests that exercise the gating
// (budgets-ai-gate, ai-forecast-refine-toggle) declare their own ``vi.mock``.
vi.mock("@/lib/hooks/use-ai-status", () => ({
  useAiStatus: () => undefined,
}));
