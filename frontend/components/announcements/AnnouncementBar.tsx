"use client";

/**
 * Operator-authored announcement banner stack.
 *
 * Mounted between the AppShell header and main content. Fetches
 * ``/api/v1/announcements`` once per AppShell mount, renders one row
 * per active+visible announcement, severity-styled. Maintenance rows
 * are force-shown (no dismiss button). Info / promo rows carry an
 * "x" button that posts to ``/api/v1/announcements/{id}/dismiss`` and
 * optimistically removes the row; a network failure re-inserts the
 * row and surfaces the alert state.
 *
 * No SWR / polling: the substrate is inert when the table is empty
 * and the next page load picks up new announcements. The pathname
 * dep refetches on route change so an operator landing a brand-new
 * announcement sees it without a hard reload.
 */
import { useCallback, useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { X, Wrench, Megaphone, Info } from "lucide-react";

import { apiFetch } from "@/lib/api";
import { linkifyAnnouncementBody } from "@/components/announcements/linkify";

export type AnnouncementSeverity = "info" | "promo" | "maintenance";

export interface Announcement {
  id: number;
  title: string;
  body: string;
  severity: AnnouncementSeverity;
  is_active: boolean;
  start_at: string | null;
  end_at: string | null;
  created_at: string;
  updated_at: string;
}

interface SeverityStyle {
  container: string;
  iconWrap: string;
  icon: React.ReactNode;
  title: string;
  link: string;
}

const SEVERITY_STYLES: Record<AnnouncementSeverity, SeverityStyle> = {
  info: {
    container: "border-border bg-surface-raised text-text-secondary",
    iconWrap: "text-text-muted",
    icon: <Info aria-hidden="true" className="h-4 w-4" strokeWidth={1.5} />,
    title: "text-text-primary",
    link: "underline text-accent hover:text-accent-hover",
  },
  promo: {
    container: "border-accent/30 bg-accent/10 text-text-primary",
    iconWrap: "text-accent",
    icon: (
      <Megaphone aria-hidden="true" className="h-4 w-4" strokeWidth={1.5} />
    ),
    title: "text-accent",
    link: "underline text-accent hover:text-accent-hover",
  },
  maintenance: {
    container: "border-warning/30 bg-warning-dim text-warning",
    iconWrap: "text-warning",
    icon: (
      <Wrench aria-hidden="true" className="h-4 w-4" strokeWidth={1.5} />
    ),
    title: "text-warning",
    link: "underline text-warning hover:text-warning-hover",
  },
};

export default function AnnouncementBar() {
  const pathname = usePathname();
  const [items, setItems] = useState<Announcement[]>([]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await apiFetch<Announcement[]>("/api/v1/announcements");
        if (cancelled) return;
        // Runtime guard — the bar is mounted globally in AppShell, so
        // a non-array payload (mock returning undefined, network blip
        // surfacing a JSON error envelope, schema drift) MUST NOT
        // crash the render. Anything that isn't an array drops into
        // the empty-items path silently.
        setItems(Array.isArray(data) ? data : []);
      } catch {
        // The bar fails silent — a broken fetch should NEVER hide the
        // app. The next route change re-fetches. We don't toast either,
        // because announcements are operator-pushed content, not user
        // actions.
        if (!cancelled) setItems([]);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  const handleDismiss = useCallback(async (id: number) => {
    // No-op when there's nothing locally to dismiss. Defensive: the
    // button only renders when items has rows, but a stale closure
    // (or a future programmatic dismissal) should still be safe.
    if (items.length === 0) return;
    // Optimistic local removal. On failure, restore.
    const snapshot = items;
    setItems((prev) => prev.filter((a) => a.id !== id));
    try {
      await apiFetch(`/api/v1/announcements/${id}/dismiss`, {
        method: "POST",
      });
    } catch {
      // Restore the row so the user can retry; no toast (we don't
      // have a global toast surface plumbed for this layer yet, and
      // a re-appearing row is itself the visible feedback signal).
      setItems(snapshot);
    }
  }, [items]);

  // Belt-and-braces — the effect's runtime guard above already coerces
  // non-array payloads to []; this second check means a future bug
  // upstream (e.g. setItems called with non-array via dev hot reload
  // or a yet-unwritten code path) still can't crash render with
  // ``items.map is not a function``.
  if (!Array.isArray(items) || items.length === 0) return null;

  return (
    <div
      data-testid="announcement-bar"
      className="flex flex-col gap-2 border-b border-border bg-bg px-4 py-2 sm:px-8"
    >
      {items.map((row) => (
        <AnnouncementRow key={row.id} row={row} onDismiss={handleDismiss} />
      ))}
    </div>
  );
}

interface AnnouncementRowProps {
  row: Announcement;
  onDismiss: (id: number) => void;
}

function AnnouncementRow({ row, onDismiss }: AnnouncementRowProps) {
  const style = SEVERITY_STYLES[row.severity];
  const dismissible = row.severity !== "maintenance";
  return (
    <div
      role={row.severity === "maintenance" ? "alert" : "status"}
      data-testid="announcement-row"
      data-severity={row.severity}
      className={`flex items-start gap-3 rounded-md border px-3 py-2 ${style.container}`}
    >
      <span className={`mt-0.5 ${style.iconWrap}`}>{style.icon}</span>
      <div className="flex-1 text-sm">
        <div className={`font-medium ${style.title}`}>{row.title}</div>
        <div className="mt-0.5 whitespace-pre-wrap text-[13px]">
          {linkifyAnnouncementBody(row.body, { linkClassName: style.link })}
        </div>
      </div>
      {dismissible && (
        <button
          type="button"
          onClick={() => onDismiss(row.id)}
          aria-label="Dismiss announcement"
          data-testid="announcement-dismiss"
          className="rounded-md p-1 text-text-muted transition-colors hover:text-text-primary"
        >
          <X aria-hidden="true" className="h-4 w-4" strokeWidth={1.5} />
        </button>
      )}
    </div>
  );
}
