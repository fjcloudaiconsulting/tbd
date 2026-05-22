"use client";

/**
 * NotificationBell — header icon + unseen-count badge + popover.
 *
 * Mounted in the AppShell header row beside the docs link / theme
 * toggle (architect lock: NOT the TrialBanner slot — those are
 * unrelated cognitive buckets).
 *
 * Data:
 * - SWR fetch ``GET /api/v1/notifications/unseen-count`` on mount —
 *   a lightweight ``{count: int}`` endpoint that does not fetch the
 *   row payload. This keeps the badge truthful when the unseen
 *   count exceeds the popover's display limit (the list fetch only
 *   pulls the most-recent 10 for the popover preview).
 * - A separate ``GET /api/v1/notifications?limit=10`` fetch backs
 *   the popover preview rows. It is mounted on the same poll cadence
 *   so the popover content is reasonably fresh when the user opens
 *   it.
 * - ``refreshInterval = 60_000`` — 60s polling matches the parent
 *   spec's "polling cadence" decision.
 * - ``revalidateOnFocus = true`` so a backgrounded tab returning to
 *   foreground picks up fresh rows immediately. The combined effect
 *   is "60s in background, instant on focus" without SSE.
 *
 * Badge:
 * - Sourced from the unseen-count endpoint (server-side
 *   ``SELECT COUNT(*) WHERE seen_at IS NULL``) — NOT from counting
 *   rows in the popover list, which is capped at 10. The wire
 *   payload returns the raw count; the bell caps the rendered label
 *   at "99+" so a future "show 250" tweak is frontend-only.
 *
 * Mark-seen:
 * - On popover-open, fire POST ``/api/v1/notifications/mark-seen``
 *   to clear ``seen_at`` for every unseen row of the current user.
 *   That is what kills the badge until new rows land. The row-level
 *   ``read_at`` state is unaffected — the inbox still shows unread
 *   rows boldly until they're individually read.
 *
 * Lucide icons:
 * - ``Bell`` when no unseen rows.
 * - ``BellRing`` when there are unseen rows (the visual mirror of
 *   the badge — accessible to color-blind users who might miss the
 *   red dot).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Bell, BellRing } from "lucide-react";

import { apiFetch } from "@/lib/api";
import type {
  NotificationListResponse,
  NotificationUnseenCountResponse,
} from "@/lib/types";

import NotificationPopover from "@/components/notifications/NotificationPopover";

const LIST_URL = "/api/v1/notifications?limit=10";
const UNSEEN_COUNT_URL = "/api/v1/notifications/unseen-count";
const MARK_SEEN_URL = "/api/v1/notifications/mark-seen";
const POLL_INTERVAL_MS = 60_000;
const BADGE_CAP = 99;

async function listFetcher(path: string): Promise<NotificationListResponse> {
  return apiFetch<NotificationListResponse>(path);
}

async function countFetcher(
  path: string,
): Promise<NotificationUnseenCountResponse> {
  return apiFetch<NotificationUnseenCountResponse>(path);
}

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Badge source — accurate even when unseen > 10 (the popover's
  // page size). A dedicated count endpoint avoids loading row
  // payloads on every poll just to compute a number.
  const { data: countData, mutate: mutateCount } =
    useSWR<NotificationUnseenCountResponse>(UNSEEN_COUNT_URL, countFetcher, {
      refreshInterval: POLL_INTERVAL_MS,
      revalidateOnFocus: true,
      revalidateOnReconnect: true,
    });

  // Popover preview rows — only fetched when the user might open
  // the popover. Still on the same poll cadence so opening doesn't
  // stall on a cold fetch.
  const { data: listData, mutate: mutateList } =
    useSWR<NotificationListResponse>(LIST_URL, listFetcher, {
      refreshInterval: POLL_INTERVAL_MS,
      revalidateOnFocus: true,
      revalidateOnReconnect: true,
    });

  const items = listData?.items ?? [];
  const unseen = countData?.count ?? 0;

  // Close-on-outside-click. The popover is a non-modal dialog — a
  // click anywhere outside the bell container collapses it. Matches
  // the existing avatar-dropdown pattern in AppShell.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!containerRef.current) return;
      if (containerRef.current.contains(e.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  // Close-on-escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const handleToggle = useCallback(async () => {
    const willOpen = !open;
    setOpen(willOpen);
    if (willOpen) {
      // Mark-seen on bell-open. Fire and forget — the badge clears
      // optimistically on the next refetch; a failure leaves the
      // badge visible (preferable to claiming "seen" when we can't
      // prove the server agrees).
      try {
        await apiFetch(MARK_SEEN_URL, { method: "POST" });
      } catch {
        // Swallow — the badge stays visible until the next poll
        // re-asserts the server state.
      }
      // Re-fetch both: count clears the badge, list refreshes the
      // popover rows' seen_at.
      mutateCount();
      mutateList();
    }
  }, [open, mutateCount, mutateList]);

  const handleAfterReadChange = useCallback(() => {
    mutateList();
  }, [mutateList]);

  // Lazy-import the popover so the initial bell render stays cheap;
  // most users will never open it. (Inline rendering — the popover
  // module is tiny, so the dynamic import would be over-engineering
  // for a single component. Direct import is fine.)
  // Imported via static path so the test environment can mock both
  // sibling files without dynamic-import gymnastics.
  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={handleToggle}
        aria-label={
          unseen > 0
            ? `Notifications, ${unseen} unseen`
            : "Notifications"
        }
        aria-haspopup="dialog"
        aria-expanded={open}
        className="relative rounded-md p-2 text-text-muted transition-colors hover:text-text-primary focus:outline-none focus:ring-1 focus:ring-accent"
        data-testid="notification-bell"
      >
        {unseen > 0 ? (
          <BellRing aria-hidden="true" className="h-5 w-5" strokeWidth={1.5} />
        ) : (
          <Bell aria-hidden="true" className="h-5 w-5" strokeWidth={1.5} />
        )}
        {unseen > 0 && (
          <span
            data-testid="notification-badge"
            className="absolute -right-0.5 -top-0.5 inline-flex min-w-[1.125rem] items-center justify-center rounded-full bg-danger px-1 text-[10px] font-semibold leading-none text-danger-text"
          >
            {unseen > BADGE_CAP ? `${BADGE_CAP}+` : unseen}
          </span>
        )}
      </button>
      {open && (
        <NotificationPopover
          items={items}
          onAfterReadChange={handleAfterReadChange}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}
