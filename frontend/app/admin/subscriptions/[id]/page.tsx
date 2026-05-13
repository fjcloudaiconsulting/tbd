"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import {
  badgeBase,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  pageTitle,
} from "@/lib/styles";
import type {
  AdminFeatureOverrideSnapshot,
  AdminSubscriptionDetail,
  SubscriptionStatus,
} from "@/lib/types";

function StatusBadge({ status }: { status: SubscriptionStatus }) {
  // Same mapping as the list page. Re-declared rather than imported
  // so the detail page stays self-contained — the list page may
  // evolve its badge into a richer affordance independently.
  const toneClass =
    status === "active"
      ? "bg-success-dim text-success"
      : status === "trialing"
        ? "bg-info-dim text-info"
        : status === "past_due"
          ? "bg-warning-dim text-warning"
          : "bg-surface-raised text-text-secondary";
  return (
    <span className={`${badgeBase} ${toneClass}`}>
      {status === "past_due" ? "past due" : status}
    </span>
  );
}

function MockBadge() {
  return (
    <span
      className="ml-1 rounded-sm bg-warning-dim px-1.5 py-px text-[10px] font-semibold uppercase tracking-wider text-warning"
      title="Payments are not live yet. Revenue figures are mocked until L2 ships."
    >
      mock
    </span>
  );
}

function FieldRow({
  label,
  value,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border-subtle py-3 last:border-0">
      <dt className="text-xs uppercase tracking-wider text-text-muted">
        {label}
      </dt>
      <dd className="text-sm text-text-primary text-right">{value ?? "—"}</dd>
    </div>
  );
}

function FeatureOverrideRow({ row }: { row: AdminFeatureOverrideSnapshot }) {
  const valueClass = row.value
    ? "bg-success-dim text-success"
    : "bg-surface-raised text-text-secondary";
  return (
    <li className="flex items-center justify-between gap-3 border-b border-border-subtle py-3 last:border-0">
      <div className="min-w-0">
        <p className="font-mono text-sm text-text-primary">{row.feature_key}</p>
        {row.note && (
          <p className="mt-1 text-xs text-text-muted">{row.note}</p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className={`${badgeBase} ${valueClass}`}>
          {row.value ? "granted" : "revoked"}
        </span>
        {row.is_expired && (
          <span
            className={`${badgeBase} bg-warning-dim text-warning`}
            title={
              row.expires_at
                ? `Expired ${row.expires_at}`
                : "Expired"
            }
          >
            expired
          </span>
        )}
      </div>
    </li>
  );
}

export default function AdminSubscriptionDetailPage() {
  const params = useParams();
  const subscriptionId = Number(params?.id);
  const { user, loading } = useAuth();
  const router = useRouter();
  const [detail, setDetail] = useState<AdminSubscriptionDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!hasPlatformPermission(user, "subscriptions.view")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  useEffect(() => {
    if (
      loading ||
      !user ||
      !hasPlatformPermission(user, "subscriptions.view") ||
      !subscriptionId
    ) {
      return;
    }
    apiFetch<AdminSubscriptionDetail>(
      `/api/v1/admin/subscriptions/${subscriptionId}`,
    )
      .then((d) => setDetail(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")));
  }, [loading, user, subscriptionId]);

  if (loading || !user || !hasPlatformPermission(user, "subscriptions.view")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <Link
            href="/admin/subscriptions"
            className="mb-2 inline-block text-xs text-text-muted hover:text-accent"
          >
            ← All subscriptions
          </Link>
          <div className="flex items-start gap-1">
            <h1 className={`${pageTitle} mb-0`}>
              {detail ? detail.org.name : "Subscription"}
            </h1>
            <HelpAnchor
              section="admin"
              label="Subscription detail"
              variant="inline-title"
            />
          </div>
        </div>
        {detail && (
          <Link
            href={`/admin/orgs/${detail.org.id}`}
            className={btnSecondary}
          >
            Override subscription
          </Link>
        )}
      </div>

      <p className="mb-6 text-sm text-text-muted">
        Read-only view. To change the plan, status, trial end, or grant
        feature overrides, use the org page linked above. Revenue figures
        are mock ($0, payments not live).
      </p>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      {!detail && !error && (
        <div className="flex min-h-[200px] items-center justify-center">
          <Spinner />
        </div>
      )}

      {detail && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <section className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Subscription</h2>
            </div>
            <dl className="px-6">
              <FieldRow
                label="Status"
                value={<StatusBadge status={detail.status} />}
              />
              <FieldRow
                label="Billing interval"
                value={detail.billing_interval}
              />
              <FieldRow label="Trial start" value={detail.trial_start} />
              <FieldRow label="Trial end" value={detail.trial_end} />
              <FieldRow
                label="Current period start"
                value={detail.current_period_start}
              />
              <FieldRow
                label="Current period end"
                value={detail.current_period_end}
              />
              <FieldRow
                label="Created"
                value={detail.created_at?.slice(0, 10)}
              />
              <FieldRow
                label={
                  <span>
                    Revenue
                    <MockBadge />
                  </span>
                }
                value={`$${detail.mock_revenue_amount}`}
              />
            </dl>
          </section>

          <section className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Organization</h2>
            </div>
            <dl className="px-6">
              <FieldRow label="Name" value={detail.org.name} />
              <FieldRow
                label="Billing cycle day"
                value={detail.org.billing_cycle_day}
              />
              <FieldRow label="Members" value={detail.org.member_count} />
              <FieldRow
                label="Created"
                value={detail.org.created_at?.slice(0, 10) ?? null}
              />
              <FieldRow
                label="Manage"
                value={
                  <Link
                    href={`/admin/orgs/${detail.org.id}`}
                    className="text-accent hover:text-accent-hover"
                  >
                    Open org →
                  </Link>
                }
              />
            </dl>
          </section>

          {detail.plan && (
            <section className={card}>
              <div className={cardHeader}>
                <h2 className={cardTitle}>Plan</h2>
              </div>
              <dl className="px-6">
                <FieldRow label="Name" value={detail.plan.name} />
                <FieldRow label="Slug" value={detail.plan.slug} />
                <FieldRow
                  label="Price (monthly)"
                  value={`$${detail.plan.price_monthly}`}
                />
                <FieldRow
                  label="Price (yearly)"
                  value={`$${detail.plan.price_yearly}`}
                />
                <FieldRow
                  label="Max users"
                  value={detail.plan.max_users ?? "unlimited"}
                />
                <FieldRow
                  label="Retention"
                  value={
                    detail.plan.retention_days !== null
                      ? `${detail.plan.retention_days} days`
                      : "unlimited"
                  }
                />
                <FieldRow
                  label="Custom plan"
                  value={detail.plan.is_custom ? "yes" : "no"}
                />
              </dl>
            </section>
          )}

          <section className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Feature overrides</h2>
            </div>
            {detail.feature_overrides.length === 0 ? (
              <p className="px-6 py-8 text-center text-sm text-text-muted">
                No overrides on this org.
              </p>
            ) : (
              <ul className="px-6 py-2">
                {detail.feature_overrides.map((o) => (
                  <FeatureOverrideRow key={o.feature_key} row={o} />
                ))}
              </ul>
            )}
            <p className="border-t border-border-subtle px-6 py-3 text-xs text-text-muted">
              Grant or revoke overrides from the{" "}
              <Link
                href={`/admin/orgs/${detail.org.id}`}
                className="text-accent hover:text-accent-hover"
              >
                org page
              </Link>
              .
            </p>
          </section>
        </div>
      )}
    </AppShell>
  );
}
