"use client";

import { useState } from "react";

import RecurrenceFields from "@/components/transactions/RecurrenceFields";
import CategorySelect from "@/components/ui/CategorySelect";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { useAccounts } from "@/lib/hooks/use-accounts";
import { useCategories } from "@/lib/hooks/use-categories";
import { useOrgCurrency } from "@/lib/hooks/use-org-currency";
import { input as inputCls, label as labelCls } from "@/lib/styles";
import type { RecurringTransaction } from "@/lib/types";

/**
 * "Edit series" (TBD-762): the whole template, from the recurring page.
 *
 * PUTs only the fields the user changed. That is load-bearing, not tidiness:
 * the server refuses a next due date that lands in a period the series already
 * has a row for, and checks the frequency/next-due pair against the billing
 * cycle, so re-sending an untouched date or frequency could refuse a rename.
 *
 * The type is not editable: income <-> expense would leave every existing row
 * on the old type and the category on the wrong side. Stop and recreate.
 *
 * Mount it with `key={item.id}`: the form state is seeded from `item` once.
 */

const MAX_OCCURRENCES = 1200;
const AMOUNT_RE = /^\d{1,10}(\.\d{1,2})?$/;

interface Props {
  item: RecurringTransaction;
  onCancel: () => void;
  onSaved: (updated: RecurringTransaction) => void;
}

export default function EditSeriesDialog({ item, onCancel, onSaved }: Props) {
  const { data: accounts } = useAccounts();
  const { data: categories, mutate: mutateCategories } = useCategories();
  const orgCurrency = useOrgCurrency();

  const [description, setDescription] = useState(item.description);
  const [amount, setAmount] = useState(String(item.amount));
  const [categoryId, setCategoryId] = useState<number | "">(item.category_id);
  const [accountId, setAccountId] = useState<number>(item.account_id);
  const [frequency, setFrequency] = useState<string>(item.frequency);
  const [nextDue, setNextDue] = useState(item.next_due_date);
  const [autoSettle, setAutoSettle] = useState(item.auto_settle);
  const [endsNever, setEndsNever] = useState(item.occurrence_count == null);
  const [count, setCount] = useState(item.occurrence_count == null ? "" : String(item.occurrence_count));
  const [submitting, setSubmitting] = useState(false);
  const [saveError, setSaveError] = useState("");
  // The page unmounts this dialog rather than closing it, so ConfirmModal's
  // own focus return (on open -> false) never runs: hand focus back to the
  // row's button on the way out. Read during the first render, before the
  // modal moves it. Not an unmount effect: StrictMode's dev double-invoke
  // would run its cleanup right after mount and pull focus out of the dialog.
  const [opener] = useState(() =>
    typeof document === "undefined" ? null : (document.activeElement as HTMLElement | null),
  );
  function cancel() {
    opener?.focus();
    onCancel();
  }

  const name = description.trim();
  const nameError = name === "" ? "Enter a name." : null;
  // Same rule as "Edit next": 12 digits with 2 decimals on the server.
  const amountError = AMOUNT_RE.test(amount) && Number(amount) > 0
    ? null
    : Number(amount) > 0
      ? "Enter an amount with up to 10 digits and 2 decimals."
      : "Enter an amount above 0.";
  const countValue = /^\d+$/.test(count.trim()) ? Number(count.trim()) : NaN;
  const countValid = countValue >= 1 && countValue <= MAX_OCCURRENCES;
  const countError = !endsNever && count.trim() !== "" && !countValid
    ? `Enter a whole number from 1 to ${MAX_OCCURRENCES}.`
    : null;
  const valid =
    nameError === null && amountError === null && categoryId !== "" && nextDue !== "" &&
    (endsNever || countValid);

  // Only what differs from the row. Amounts are Decimal strings on the wire
  // ("1200.00"): compare as numbers, send what the user typed.
  const changes: Record<string, unknown> = {};
  if (name !== item.description) changes.description = name;
  if (amountError === null && Number(amount) !== Number(item.amount)) changes.amount = amount;
  if (categoryId !== "" && categoryId !== item.category_id) changes.category_id = categoryId;
  if (accountId !== item.account_id) changes.account_id = accountId;
  if (frequency !== item.frequency) changes.frequency = frequency;
  if (nextDue !== "" && nextDue !== item.next_due_date) changes.next_due_date = nextDue;
  if (autoSettle !== item.auto_settle) changes.auto_settle = autoSettle;
  // An explicit null is "never ends"; an omitted key leaves the count alone.
  const newCount = endsNever ? null : countValid ? countValue : item.occurrence_count;
  if (newCount !== item.occurrence_count) changes.occurrence_count = newCount;
  const changed = Object.keys(changes).length > 0;

  // The count is a TOTAL including what is already done (TBD-275).
  const shownTotal = countValid ? countValue : item.occurrence_count;
  const progress = !endsNever && shownTotal != null
    ? `${item.occurrences_elapsed} of ${shownTotal} done.` +
      (shownTotal <= item.occurrences_elapsed ? " No more occurrences will be created." : "")
    : null;

  // The org currency, when the client knows it (one currency across every
  // account). A legacy mixed-currency org has none on the client, so the
  // series' current account stands in.
  const accountList = accounts ?? [];
  const currencyOf = (id: number) =>
    accountList.find((a) => a.id === id)?.currency?.toUpperCase();
  const reference = orgCurrency?.toUpperCase() ?? currencyOf(item.account_id);
  const chosen = currencyOf(accountId);
  const currencyWarning = chosen && reference && chosen !== reference
    ? `This account is in ${chosen}. Forecasts and totals count ${reference} only, so this series will drop out of them.`
    : null;
  // Inactive accounts are not offered, except the one the series is on.
  const accountOptions = accountList.filter((a) => a.is_active || a.id === item.account_id);

  async function save() {
    if (!valid || !changed || submitting) return;
    setSubmitting(true);
    setSaveError("");
    try {
      const updated = await apiFetch<RecurringTransaction>(`/api/v1/recurring/${item.id}`, {
        method: "PUT",
        body: JSON.stringify(changes),
      });
      opener?.focus();
      onSaved(updated);
    } catch (err) {
      setSaveError(extractErrorMessage(err));
      setSubmitting(false);
    }
  }

  return (
    <ConfirmModal
      open
      title="Edit Series"
      message={`Changes apply to "${item.description}" from its next occurrence on.`}
      confirmLabel="Save changes"
      submitting={submitting}
      confirmDisabled={!valid || !changed}
      onConfirm={save}
      onCancel={cancel}
    >
      <div className="mt-4 flex flex-col gap-4">
        <div>
          <label htmlFor="edit-series-name" className={labelCls}>Name</label>
          <input
            id="edit-series-name"
            type="text"
            maxLength={255}
            // Save starts disabled, so ConfirmModal's focus-the-confirm
            // fallback cannot take focus into the dialog; the first field does.
            autoFocus
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            aria-invalid={nameError ? true : undefined}
            aria-describedby={nameError ? "edit-series-name-error" : undefined}
            className={inputCls}
          />
          {nameError && (
            <p id="edit-series-name-error" className="mt-1 text-xs text-danger">{nameError}</p>
          )}
        </div>
        <div>
          <label htmlFor="edit-series-amount" className={labelCls}>Amount</label>
          <input
            id="edit-series-amount"
            type="number"
            step="0.01"
            min="0.01"
            inputMode="decimal"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            aria-invalid={amountError ? true : undefined}
            aria-describedby={amountError ? "edit-series-amount-error" : undefined}
            className={inputCls}
          />
          {amountError && (
            <p id="edit-series-amount-error" className="mt-1 text-xs text-danger">{amountError}</p>
          )}
        </div>
        <div>
          <label htmlFor="edit-series-category" className={labelCls}>Category</label>
          <CategorySelect
            id="edit-series-category"
            categories={categories ?? []}
            value={categoryId}
            onChange={setCategoryId}
            filterType={item.type}
            className={inputCls}
            onCategoryCreated={(c) =>
              mutateCategories((current) => [...(current ?? []), c], { revalidate: false })
            }
          />
        </div>
        <div>
          <label htmlFor="edit-series-account" className={labelCls}>Account</label>
          <select
            id="edit-series-account"
            value={accountId}
            onChange={(e) => setAccountId(Number(e.target.value))}
            aria-describedby={currencyWarning ? "edit-series-currency" : undefined}
            className={inputCls}
          >
            {accountOptions.length === 0 && <option value={item.account_id}>{item.account_name}</option>}
            {accountOptions.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
          {currencyWarning && (
            <p id="edit-series-currency" className="mt-1 text-xs text-warning">{currencyWarning}</p>
          )}
        </div>
        <div>
          {/* TBD-301: no `min`. The server's floor is the org's cycle start,
              not today, so a today-floored picker greys out legal dates. */}
          <label htmlFor="edit-series-next-due" className={labelCls}>Next due date</label>
          <input
            id="edit-series-next-due"
            type="date"
            value={nextDue}
            onChange={(e) => setNextDue(e.target.value)}
            className={inputCls}
          />
        </div>
        <RecurrenceFields
          variant="stacked"
          idPrefix="edit-series"
          frequency={frequency}
          onFrequencyChange={setFrequency}
          occurrenceCount={count}
          onOccurrenceCountChange={setCount}
          autoSettle={autoSettle}
          onAutoSettleChange={setAutoSettle}
          endsNever={endsNever}
          onEndsNeverChange={setEndsNever}
          progress={progress}
          countError={countError}
        />
        <p className="text-xs text-text-muted">
          Pending occurrences already created follow a new name, category, account or amount,
          unless you changed them by hand. Paid occurrences never change. A new frequency,
          next due date or number of occurrences applies from the next occurrence:
          occurrences already created keep their dates, so skip or delete any that no longer
          apply. To switch between income and expense, stop this series and create a new one.
        </p>
        {saveError && (
          <p role="alert" className="text-sm text-danger">{saveError}</p>
        )}
      </div>
    </ConfirmModal>
  );
}
