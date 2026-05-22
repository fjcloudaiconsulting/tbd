"use client";

/**
 * NotificationBell — header icon + unseen-count badge + popover.
 *
 * Mounted in the AppShell header row beside the docs link / theme
 * toggle (architect lock: NOT the TrialBanner slot — those are
 * unrelated cognitive buckets).
 *
 * Data:
 * - SWR fetch ``GET /api/v1/notifications?limit=10`` on mount.
 * - ``refreshInterval = 60_000`` — 60s polling matches the parent
 *   spec's "polling cadence" decision.
 * - ``revalidateOnFocus = true`` so a backgrounded tab returning to
 *   foreground picks up fresh rows immediately. The combined effect
 *   is "60s in background, instant on focus" without SSE.
 *
 * Badge:
 * - Counts rows where ``seen_at === null`` (the "unseen" channel —
 *   distinct from "unread"). The 2nd-arch delta locked this two-
 *   column model so the badge clears on bell-open even if the user
 *   never clicks individual rows.
 * - Caps the visible label at "99+" per the parent spec's
 *   pagination decision (G3).
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
import type { NotificationListResponse } from "@/lib/types";

import NotificationPopover from "@/components/notifications/NotificationPopover";

const FETCH_URL = "/api/v1/notifications?limit=10";
const MARK_SEEN_URL = "/api/v1/notifications/mark-seen";
const POLL_INTERVAL_MS = 60_000;
const BADGE_CAP = 99;

async function fetcher(path: string): Promise<NotificationListResponse> {
  return apiFetch<NotificationListResponse>(path);
}

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const { data, mutate } = useSWR<NotificationListResponse>(
    FETCH_URL,
    fetcher,
    {
      refreshInterval: POLL_INTERVAL_MS,
      revalidateOnFocus: true,
      revalidateOnReconnect: true,
      // SWR returns undefined on first render; the bell + count
      // gracefully treats undefined as "no data yet, no badge".
    },
  );

  const items = data?.items ?? [];
  const unseen = items.filter((it) => it.seen_at === null).length;

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
      // Re-fetch so the popover renders rows with fresh seen_at.
      mutate();
    }
  }, [open, mutate]);

  const handleAfterReadChange = useCallback(() => {
    mutate();
  }, [mutate]);

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
            className="absolute -right-0.5 -top-0.5 inline-flex min-w-[1.125rem] items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-semibold leading-none text-white"
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
