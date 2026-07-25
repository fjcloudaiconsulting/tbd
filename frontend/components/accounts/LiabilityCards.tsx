"use client";

/**
 * LiabilityCards — the detail zone below the accounts table. The list itself
 * stays a clean one-line-per-account balance glance; the rich CC / loan facts
 * live here as compact financial stat blocks, one card per liability, grouped
 * by type. See specs/2026-07-24-loan-account-type-v1-design.md (accounts
 * redesign) + the two design/architect reviews.
 *
 * Composition (both card types): account name (quiet label) -> balance hero
 * -> ONE expressive element (CC utilization bar / loan payoff chip, the only
 * colored moment; gold stays on the page CTA per the One Brass Rule) ->
 * hairline divider -> a BORDERLESS 2-col label/value metric list (hierarchy
 * from weight + spacing, never internal gridlines, which would just relocate
 * the spreadsheet skin). Metric labels use text-secondary (a step stronger
 * than text-muted) for hierarchy against the balance hero; the zone heading
 * below uses text-muted via the shared cardTitle token (it clears AA on both
 * themes since the token was darkened).
 */
import CreditUtilizationBar from "@/components/dashboard/widgets/CreditUtilizationBar";
import { creditUtilization } from "@/lib/credit";
import { formatAmount, formatMonthYear } from "@/lib/format";
import { badgeInfo, badgeNeutral, badgeSuccess, badgeWarning, cardTitle } from "@/lib/styles";
import type { Account } from "@/lib/types";

interface PaymentSource {
  name: string;
  isActive: boolean;
}

/** Resolve a liability's "paid from" source account (org-scoped list). Shared
 *  so the cards don't re-implement the lookup. */
export function resolvePaymentSource(
  accounts: Account[],
  sourceId: number | null | undefined,
): PaymentSource | null {
  if (sourceId == null) return null;
  const src = accounts.find((a) => a.id === sourceId);
  if (!src) return { name: "unknown", isActive: true };
  return { name: src.name, isActive: src.is_active };
}

function ordinalDay(day: number): string {
  const mod100 = day % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${day}th`;
  switch (day % 10) {
    case 1:
      return `${day}st`;
    case 2:
      return `${day}nd`;
    case 3:
      return `${day}rd`;
    default:
      return `${day}th`;
  }
}

function Metric({
  label,
  value,
  numeric = false,
}: {
  label: string;
  value: React.ReactNode;
  numeric?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="mb-0.5 text-xs font-semibold uppercase tracking-[0.08em] text-text-secondary">
        {label}
      </dt>
      <dd className={`truncate text-sm text-text-primary${numeric ? " tabular-nums" : ""}`}>
        {value}
      </dd>
    </div>
  );
}

function PaidFrom({ source }: { source: PaymentSource | null }) {
  if (!source) return <Metric label="Paid from" value="Not set" />;
  return (
    <Metric
      label="Paid from"
      value={
        <>
          {source.name}
          {!source.isActive ? <span className="text-danger"> (inactive)</span> : null}
        </>
      }
    />
  );
}

function CardShell({
  account,
  testid,
  expressive,
  children,
}: {
  account: Account;
  testid: string;
  expressive: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div
      data-testid={testid}
      className={`flex flex-col gap-3 rounded-lg border border-border bg-surface p-4${
        account.is_active ? "" : " opacity-50"
      }`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-xs text-text-secondary">{account.name}</span>
        {!account.is_active ? (
          <span className="shrink-0 text-xs text-danger">inactive</span>
        ) : null}
      </div>
      <div className="text-2xl font-semibold tabular-nums text-text-primary">
        {formatAmount(account.balance)}{" "}
        <span className="text-base font-normal text-text-secondary">{account.currency}</span>
      </div>
      {expressive}
      {children ? (
        <>
          <div className="border-t border-border" />
          <dl className="grid grid-cols-2 gap-x-4 gap-y-3">{children}</dl>
        </>
      ) : null}
    </div>
  );
}

function CreditCardCard({ account, accounts }: { account: Account; accounts: Account[] }) {
  const hasLimit = Number(account.credit_limit) > 0;
  const source = resolvePaymentSource(accounts, account.payment_source_account_id);
  return (
    <CardShell
      account={account}
      testid={`cc-card-${account.id}`}
      expressive={
        hasLimit ? (
          <CreditUtilizationBar
            hideName
            name={account.name}
            balance={Number(account.balance)}
            creditLimit={Number(account.credit_limit)}
            currency={account.currency}
          />
        ) : (
          <span className="text-xs text-text-secondary">No credit limit set</span>
        )
      }
    >
      <Metric
        label="Statement closes"
        value={account.close_day ? ordinalDay(account.close_day) : "Not set"}
      />
      <Metric
        label="Credit limit"
        value={hasLimit ? `${formatAmount(account.credit_limit as number)} ${account.currency}` : "Not set"}
        numeric={hasLimit}
      />
      <PaidFrom source={source} />
    </CardShell>
  );
}

function LoanCard({ account, accounts }: { account: Account; accounts: Account[] }) {
  const source = resolvePaymentSource(accounts, account.payment_source_account_id);
  const m = account.loan;

  let chip: React.ReactNode;
  if (!m) {
    chip = <span className={badgeInfo}>Finish setting up this loan</span>;
  } else if (m.status === "paid_off") {
    chip = <span className={badgeNeutral}>Paid off</span>;
  } else if (m.status === "interest_only") {
    chip = <span className={badgeWarning}>Payment covers interest only</span>;
  } else {
    // on_track: the backend supplies a payoff date, but guard defensively so a
    // null never renders a dangling "paid off by ".
    chip = (
      <span className={badgeSuccess}>
        {m.projected_payoff_date
          ? `On track · paid off by ${formatMonthYear(m.projected_payoff_date)}`
          : "On track"}
      </span>
    );
  }

  return (
    <CardShell
      account={account}
      testid={`loan-card-${account.id}`}
      expressive={<div>{chip}</div>}
    >
      {m ? (
        <>
          <Metric
            label="Monthly payment"
            value={`${formatAmount(m.expected_monthly_payment)} ${account.currency}`}
            numeric
          />
          <Metric
            label="Rate"
            value={account.interest_rate_apr != null ? `${Number(account.interest_rate_apr)}%` : "—"}
            numeric
          />
          <Metric
            label="Term"
            value={account.term_months != null ? `${account.term_months} mo` : "—"}
            numeric
          />
          <Metric label="Matures" value={formatMonthYear(m.maturation_date)} />
          <Metric
            label="Interest over term"
            value={`${formatAmount(m.total_interest)} ${account.currency}`}
            numeric
          />
          <PaidFrom source={source} />
        </>
      ) : null}
    </CardShell>
  );
}

export default function LiabilityCards({ accounts }: { accounts: Account[] }) {
  // Credit cards first (by utilization desc), then loans (by balance magnitude
  // desc), rendered in ONE responsive grid capped at 3 columns on large screens
  // (2 on tablet, 1 on mobile). A grid (not flex-wrap) keeps every card an equal
  // fraction and, as accounts grow, wraps extras into tidy rows of 3 without
  // stretching a lone last-row card full-width. One zone heading labels the
  // whole band (below), but the card content (utilization bar vs payoff chip)
  // already signals the type, so no per-type sub-headings inside it.
  const creditCards = accounts
    .filter((a) => a.account_type_slug === "credit_card")
    .sort(
      (x, y) =>
        creditUtilization(Number(y.balance), Number(y.credit_limit)).utilizationPct -
        creditUtilization(Number(x.balance), Number(x.credit_limit)).utilizationPct,
    );
  const loans = accounts
    .filter((a) => a.account_type_slug === "loan")
    .sort((x, y) => Math.abs(Number(y.balance)) - Math.abs(Number(x.balance)));

  if (creditCards.length === 0 && loans.length === 0) return null;

  // One zone label so a single card no longer reads as attached to the
  // Account Types column above-left; it anchors the cards as their own
  // full-width band. Contextual (cards / loans / both) but never per-type
  // sub-headings — the utilization bar vs payoff chip already signals type.
  const heading =
    creditCards.length > 0 && loans.length > 0
      ? "Credit cards & loans"
      : creditCards.length > 0
        ? "Credit cards"
        : "Loans";

  return (
    <section data-testid="liability-cards" className="mt-8" aria-labelledby="liability-cards-heading">
      <h2 id="liability-cards-heading" data-testid="liability-cards-heading" className={`mb-3 ${cardTitle}`}>
        {heading}
      </h2>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {creditCards.map((a) => (
          <CreditCardCard key={a.id} account={a} accounts={accounts} />
        ))}
        {loans.map((a) => (
          <LoanCard key={a.id} account={a} accounts={accounts} />
        ))}
      </div>
    </section>
  );
}
