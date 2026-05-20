"use client";

import { FormEvent, MouseEvent, useEffect, useRef, useState } from "react";

import DescriptionAutocomplete from "@/components/transactions/DescriptionAutocomplete";
import TagChipInput from "@/components/transactions/TagChipInput";
import CategorySelect from "@/components/ui/CategorySelect";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { todayISO } from "@/lib/format";
import {
  btnPrimary,
  btnSecondary,
  error as errorCls,
  input,
  label,
} from "@/lib/styles";
import type { Account, Category, Transaction } from "@/lib/types";

/**
 * Quick-entry transaction form used inside the AppShell-level Add
 * Transaction CTA's SlideInPanel.
 *
 * NOTE, duplication tech debt: the canonical add-transaction form
 * still lives inline inside `frontend/app/transactions/page.tsx`. The
 * Dashboard's inline Quick Add form was removed when the AppShell CTA
 * shipped, so the Transactions page is now the only remaining inline
 * caller. Extracting it would touch files recently in flight, so this
 * component ships a focused subset (transaction-only, no transfer
 * mode, no edit-time promote-to-recurring) sized for quick entry. A
 * follow-up should fold the page-level form into this component.
 *
 * Scope:
 *   - Transaction-only (no transfer mode in the quick-entry panel;
 *     transfers belong on the Transactions page where both legs are
 *     visible).
 *   - Posts to POST /api/v1/transactions exactly like the page-level
 *     form. No new backend endpoints.
 *   - Repeats / promote-to-recurring: deferred to a later iteration.
 *     The primary use case is "I just spent something, log it."
 *
 * "Save & add new" behavior:
 *   - Default Save submits + closes the panel via onSaved().
 *   - "Save & add new" submits + clears the form fields, leaves the
 *     panel open, and refocuses the description input.
 *   - Both buttons route through the same form onSubmit handler so
 *     native HTML5 validation (the `required` attributes, `min` on
 *     amount, etc.) runs before any network call. "Save and add new"
 *     uses an intent ref + form.requestSubmit() so the browser shows
 *     its built-in validation messages and skips the submit when a
 *     required field is empty.
 */

export interface TransactionFormProps {
  accounts: Account[];
  categories: Category[];
  /** Pre-selected account id (default-from-context). */
  defaultAccountId?: number | null;
  /** Pre-selected category id (default-from-context). */
  defaultCategoryId?: number | null;
  /** Called after a successful Save (panel-close path). */
  onSaved: () => void;
  /** Called after a category is created via the inline modal. */
  onCategoryCreated?: (category: Category) => void;
  /**
   * Called after every successful save (Save and Save & add new).
   * Pages can use this to refresh transaction lists.
   */
  onTransactionAdded?: () => void;
  /**
   * Called when the transaction was saved but a follow-up sub-call
   * (e.g. PUT /tags) failed. The base transaction IS committed; the
   * caller should surface this as a non-blocking warning instead of
   * a hard error so the user does not retry and double-create. See
   * the partial-success handling in submit() below.
   */
  onWarning?: (msg: string) => void;
}

export default function TransactionForm({
  accounts,
  categories,
  defaultAccountId = null,
  defaultCategoryId = null,
  onSaved,
  onCategoryCreated,
  onTransactionAdded,
  onWarning,
}: TransactionFormProps) {
  const activeAccounts = accounts.filter((a) => a.is_active);
  const fallbackAccount = activeAccounts.find((a) => a.is_default) ?? activeAccounts[0];
  const initialAccountId =
    defaultAccountId ?? (fallbackAccount ? fallbackAccount.id : "");

  const [accountId, setAccountId] = useState<number | "">(initialAccountId ?? "");
  const [categoryId, setCategoryId] = useState<number | "">(
    defaultCategoryId ?? "",
  );
  const [description, setDescription] = useState("");
  const [amount, setAmount] = useState("");
  const [type, setType] = useState<"income" | "expense">("expense");
  const [status, setStatus] = useState<"settled" | "pending">(() => {
    const acct = activeAccounts.find((a) => a.id === initialAccountId);
    return acct?.account_type_slug === "credit_card" ? "pending" : "settled";
  });
  const [date, setDate] = useState(todayISO());
  // Expected settlement date for pending creates. Left empty by default so
  // the user explicitly picks a settlement date when status=pending; this
  // mirrors the canonical /transactions create form (PR #197) and keeps
  // credit-card-style settlement lag a deliberate choice instead of
  // silently inheriting the transaction date.
  const [settledDate, setSettledDate] = useState("");
  // PR-Tags-A: chip-managed tag set. Normalized lowercase names. The
  // backend auto-creates new tags via PUT /api/v1/transactions/{id}/tags
  // (called right after the POST below) so the user does not need to
  // pre-create tags from a management page.
  const [tags, setTags] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [errMsg, setErrMsg] = useState("");
  // Focus target for the "Save and add new" refocus path. The
  // DescriptionAutocomplete component renders its own <input
  // id="fab-tx-desc"> so we look it up by id (the component owns the
  // ref internally; we don't need to thread one through).
  const focusDescription = () => {
    const el = document.getElementById("fab-tx-desc");
    if (el instanceof HTMLInputElement) el.focus();
  };
  // Tracks whether the next submit should keep the panel open and clear
  // the form ("Save and add new") or close on success ("Save"). The
  // "Save and add new" button calls form.requestSubmit() so the browser
  // runs native validation before the onSubmit handler reads this.
  const submitIntentRef = useRef<"save" | "save-and-add-new">("save");

  // If the parent updates the defaults (e.g. accounts finished loading
  // after the panel mounted), pick them up so the form isn't stuck on
  // an empty selection.
  useEffect(() => {
    if (accountId === "" && initialAccountId) setAccountId(initialAccountId);
    // Intentionally not reacting to subsequent activeAccounts changes,
    // we don't want to overwrite a user's manual selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialAccountId]);

  function handleAccountChange(id: number | "") {
    setAccountId(id);
    const acct = activeAccounts.find((a) => a.id === id);
    setStatus(acct?.account_type_slug === "credit_card" ? "pending" : "settled");
  }

  function handleTypeChange(t: "income" | "expense") {
    setType(t);
    setCategoryId("");
  }

  function clearForm() {
    setDescription("");
    setAmount("");
    // Keep account selection, most users add multiple txs to the same
    // account in one sitting. Reset category since type may change.
    setCategoryId(defaultCategoryId ?? "");
    setType("expense");
    setDate(todayISO());
    // Clear the optional pending settled-date so the next entry starts
    // fresh. Status default re-derives from the (preserved) account on
    // its own, so we leave that alone.
    setSettledDate("");
    // Tags are per-transaction, not sticky across entries.
    setTags([]);
    setErrMsg("");
  }

  async function submit(addAnother: boolean) {
    // Inline validation for the optional pending settled-date field.
    // Mirrors the canonical /transactions create form (PR #197) and the
    // backend cross-field check; validating client-side keeps the form
    // submit experience snappy when the user picks an earlier date.
    if (status === "pending" && settledDate && settledDate < date) {
      setErrMsg(
        "Expected settlement date must be on or after the transaction date",
      );
      return;
    }
    setSubmitting(true);
    setErrMsg("");
    // Step 1: POST the base transaction. Failures here keep the form
    // open with the user's input intact so they can correct + retry.
    let created: Transaction | undefined;
    try {
      created = await apiFetch<Transaction>("/api/v1/transactions", {
        method: "POST",
        body: JSON.stringify({
          account_id: accountId,
          category_id: categoryId,
          description,
          amount,
          type,
          status,
          date,
          // settled_date only travels on pending creates with a value
          // set; settled rows get their settled_date stamped server-side
          // from `date` (PR #197 contract).
          ...(status === "pending" && settledDate
            ? { settled_date: settledDate }
            : {}),
        }),
      });
    } catch (err) {
      setErrMsg(extractErrorMessage(err));
      setSubmitting(false);
      return;
    }
    // Step 2: PUT /tags. The transaction was already saved, so any
    // failure here is a PARTIAL SUCCESS — we must NOT leave the form
    // in a state that re-POSTs the base transaction on the next
    // click, or we double-create. Close + reset the form and surface
    // a non-blocking warning via onWarning so the parent can show a
    // "Transaction saved, tags couldn't be applied" banner.
    let tagWarning: string | null = null;
    if (tags.length > 0 && created?.id) {
      try {
        await apiFetch(`/api/v1/transactions/${created.id}/tags`, {
          method: "PUT",
          body: JSON.stringify({ tag_names: tags }),
        });
      } catch (err) {
        tagWarning = `Transaction saved. Tags couldn't be applied: ${extractErrorMessage(
          err,
        )}. Edit the transaction to add them.`;
      }
    }
    onTransactionAdded?.();
    if (tagWarning) {
      onWarning?.(tagWarning);
    }
    if (addAnother) {
      clearForm();
      // Refocus the description input via the DescriptionAutocomplete
      // component's exposed focus path (id-based lookup; the component
      // owns its own input element). Preserves the post-PR-#325 focus
      // contract instead of the pre-#325 descRef pattern.
      window.setTimeout(() => focusDescription(), 0);
    } else {
      onSaved();
    }
    setSubmitting(false);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const addAnother = submitIntentRef.current === "save-and-add-new";
    // Reset eagerly so a later plain Save submit defaults to closing the
    // panel. The async submit() reads `addAnother` from the local above.
    submitIntentRef.current = "save";
    void submit(addAnother);
  }

  function handleSaveAndAddNewClick(e: MouseEvent<HTMLButtonElement>) {
    submitIntentRef.current = "save-and-add-new";
    // requestSubmit() triggers the form's native validation (required,
    // min, type=number, etc.) and only fires onSubmit when the form is
    // valid. If validation fails, the browser shows its built-in
    // messages and no network call happens.
    const form = e.currentTarget.form;
    if (form) {
      form.requestSubmit();
    } else {
      // Fallback for the rare case the button is rendered outside a
      // form (shouldn't happen here, but keeps the handler safe).
      submitIntentRef.current = "save";
    }
  }

  const hasAccountsAndCategories =
    activeAccounts.length > 0 && categories.length > 0;

  if (!hasAccountsAndCategories) {
    return (
      <div className="rounded-md border border-border bg-surface-raised p-4 text-sm text-text-secondary">
        Create at least one account and one category before adding
        transactions.
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      {errMsg && <div className={errorCls}>{errMsg}</div>}

      <div>
        <label htmlFor="fab-tx-account" className={label}>
          Account
        </label>
        <select
          id="fab-tx-account"
          required
          value={accountId}
          onChange={(e) =>
            handleAccountChange(e.target.value === "" ? "" : Number(e.target.value))
          }
          className={input}
        >
          <option value="">Select account</option>
          {activeAccounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label htmlFor="fab-tx-type" className={label}>
          Type
        </label>
        <select
          id="fab-tx-type"
          value={type}
          onChange={(e) => handleTypeChange(e.target.value as "income" | "expense")}
          className={input}
        >
          <option value="expense">Expense</option>
          <option value="income">Income</option>
        </select>
      </div>

      <div>
        <label htmlFor="fab-tx-category" className={label}>
          Category
        </label>
        <CategorySelect
          id="fab-tx-category"
          categories={categories}
          value={categoryId}
          onChange={setCategoryId}
          filterType={type}
          className={input}
          onCategoryCreated={(cat) => onCategoryCreated?.(cat)}
        />
      </div>

      <div>
        <label htmlFor="fab-tx-desc" className={label}>
          Description
        </label>
        <DescriptionAutocomplete
          id="fab-tx-desc"
          type={type}
          value={description}
          onChange={setDescription}
          onPick={(s) => {
            // Pre-fill category from the most-common pair for this
            // description, but only if the user has not already
            // chosen one. Matches the canonical /transactions add
            // form (page.tsx) and the spec's "optional pre-populate"
            // rule for the category hint.
            if (categoryId === "") {
              setCategoryId(s.category_id);
            }
          }}
          placeholder="What was it for?"
          required
          ariaLabel="Description"
        />
      </div>

      <div>
        <label htmlFor="fab-tx-tags" className={label}>
          Tags
        </label>
        <TagChipInput
          id="fab-tx-tags"
          value={tags}
          onChange={setTags}
          categoryId={categoryId}
          disabled={submitting}
        />
      </div>

      <div>
        <label htmlFor="fab-tx-amount" className={label}>
          Amount
        </label>
        <input
          id="fab-tx-amount"
          type="number"
          step="0.01"
          min="0.01"
          required
          placeholder="0.00"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          className={input}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor="fab-tx-status" className={label}>
            Status
          </label>
          <select
            id="fab-tx-status"
            value={status}
            onChange={(e) => setStatus(e.target.value as "settled" | "pending")}
            className={input}
          >
            <option value="settled">Settled</option>
            <option value="pending">Pending</option>
          </select>
        </div>
        <div>
          <label htmlFor="fab-tx-date" className={label}>
            Date
          </label>
          <input
            id="fab-tx-date"
            type="date"
            required
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className={input}
          />
        </div>
      </div>

      {status === "pending" && (
        <div>
          <label htmlFor="fab-tx-settled-date" className={label}>
            Expected settlement date
          </label>
          <input
            id="fab-tx-settled-date"
            type="date"
            min={date}
            value={settledDate}
            onChange={(e) => setSettledDate(e.target.value)}
            className={input}
          />
          <p className="mt-1 text-[10px] text-text-muted">
            Optional. When the bank actually charges the card.
          </p>
        </div>
      )}

      <div className="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
        <button
          type="button"
          onClick={handleSaveAndAddNewClick}
          disabled={submitting}
          className={`${btnSecondary} min-h-[44px]`}
        >
          Save and add new
        </button>
        <button
          type="submit"
          disabled={submitting}
          className={btnPrimary}
        >
          {submitting ? "Saving..." : "Save"}
        </button>
      </div>
    </form>
  );
}
