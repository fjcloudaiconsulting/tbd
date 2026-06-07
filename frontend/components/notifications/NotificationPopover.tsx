"use client";

/**
 * NotificationPopover — dropdown that lists the most-recent 10
 * notifications for the current user.
 *
 * Mounted by ``<NotificationBell />`` when the bell is clicked.
 * Owns no fetch state of its own; the bell passes ``items`` down so
 * the popover stays a pure render component (and bell-open does not
 * trigger a second network round-trip).
 *
 * Per-row interaction:
 * - Click → PATCH ``/api/v1/notifications/{id}`` with ``{read: true}``
 *   to mark read, then navigate to ``link_url`` if present.
 * - The list re-renders without that row's bold "unread" treatment
 *   on the next refetch (mark-read is in-band; SWR mutate is the
 *   caller's job in the bell).
 *
 * Severity dot — visual category cue:
 * - security → subtle red dot (urgent / actionable)
 * - account / org_admin → neutral muted dot (informational)
 * - org_activity → muted further (background noise, opt-in only)
 *
 * "See all" footer link points to ``/settings/notifications`` —
 * that page lands in PR5 of the train. Until then it renders as a
 * disabled-looking text link rather than a hard 404 so we don't ship
 * a dangling anchor.
 */
import { useRouter } from "next/navigation";
import { useCallback } from "react";

import { apiFetch } from "@/lib/api";
import type { Notification, NotificationCategory } from "@/lib/types";

interface Props {
  items: Notification[];
  /** Called after a row is marked read so the parent can re-fetch. */
  onAfterReadChange: () => void;
  /** Close handler — fires after navigation so the popover collapses. */
  onClose: () => void;
}

const SEVERITY_DOT: Record<NotificationCategory, string> = {
  security: "bg-danger",
  account: "bg-text-muted",
  org_admin: "bg-text-muted",
  org_activity: "bg-text-muted/40",
};

function timeAgo(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffSec = Math.max(0, Math.floor((now - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  const min = Math.floor(diffSec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const days = Math.floor(hr / 24);
  return `${days}d ago`;
}

export default function NotificationPopover({
  items,
  onAfterReadChange,
  onClose,
}: Props) {
  const router = useRouter();

  const handleRowClick = useCallback(
    async (notif: Notification) => {
      // Optimistic UX: mark read in the background, navigate
      // immediately. Failure is swallowed — the next refetch will
      // resync.
      apiFetch<Notification>(`/api/v1/notifications/${notif.id}`, {
        method: "PATCH",
        body: JSON.stringify({ read: true }),
      })
        .catch(() => {
          // no-op — best effort. The badge / unread state will
          // self-heal on the next refresh.
        })
        .finally(() => {
          onAfterReadChange();
        });

      if (notif.link_url) {
        router.push(notif.link_url);
      }
      onClose();
    },
    [router, onAfterReadChange, onClose],
  );

  if (items.length === 0) {
    return (
      <div
        role="dialog"
        aria-label="Notifications"
        className="absolute right-0 top-full z-50 mt-2 w-80 rounded-lg border border-border bg-surface shadow-xl"
      >
        <div className="px-4 py-3 text-sm font-medium text-text-primary">
          Notifications
        </div>
        <div className="border-t border-border" />
        <div className="px-4 py-8 text-center text-sm text-text-muted">
          You&apos;re all caught up.
        </div>
        <div className="border-t border-border" />
        <FooterLink />
      </div>
    );
  }

  // Cap at 10 — the popover never shows more than the latest 10. The
  // bell already requests limit=10 from the backend, so this slice is
  // a belt-and-braces guard against a future caller passing more.
  const visible = items.slice(0, 10);

  return (
    <div
      role="dialog"
      aria-label="Notifications"
      className="absolute right-0 top-full z-50 mt-2 w-80 rounded-lg border border-border bg-surface shadow-xl"
    >
      <div className="px-4 py-3 text-sm font-medium text-text-primary">
        Notifications
      </div>
      <div className="border-t border-border" />
      <ul className="max-h-96 overflow-y-auto" data-testid="notification-list">
        {visible.map((notif) => (
          <li
            key={notif.id}
            className={
              "border-b border-border last:border-b-0 " +
              (notif.read_at === null ? "bg-surface" : "bg-surface-raised/40")
            }
          >
            <button
              type="button"
              onClick={() => handleRowClick(notif)}
              className="flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
            >
              <span
                aria-hidden="true"
                data-testid={`severity-dot-${notif.category}`}
                className={
                  "mt-1.5 inline-block h-2 w-2 shrink-0 rounded-full " +
                  SEVERITY_DOT[notif.category]
                }
              />
              <span className="flex-1 min-w-0">
                <span className="flex items-baseline justify-between gap-2">
                  <span
                    className={
                      "block truncate text-sm " +
                      (notif.read_at === null
                        ? "font-medium text-text-primary"
                        : "text-text-secondary")
                    }
                  >
                    {notif.title}
                  </span>
                  <span className="shrink-0 text-xs text-text-muted">
                    {timeAgo(notif.created_at)}
                  </span>
                </span>
                <span className="mt-0.5 block line-clamp-2 text-xs text-text-muted">
                  {notif.body}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
      <div className="border-t border-border" />
      <FooterLink />
    </div>
  );
}

function FooterLink() {
  // The full inbox page (/settings/notifications) lands in PR5. Until
  // then we render the link as a disabled-looking element so we don't
  // ship a 404 anchor. When PR5 lands, swap this for a real <Link />.
  return (
    <div className="px-4 py-2 text-center text-xs text-text-muted">
      <span aria-disabled="true" title="Available in a future update">
        View all (coming soon)
      </span>
    </div>
  );
}
