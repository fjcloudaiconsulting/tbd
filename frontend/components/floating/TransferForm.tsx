"use client";

import { FormEvent, MouseEvent, useEffect, useRef, useState } from "react";

import CategorySelect from "@/components/ui/CategorySelect";
import HelpAnchor from "@/components/HelpAnchor";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { todayISO } from "@/lib/format";
import {
  btnPrimary,
  btnSecondary,
  error as errorCls,
  input,
  label,
} from "@/lib/styles";
import type { Account, Category } from "@/lib/types";

/**
 * Quick-entry transfer form used inside the AppShell-level quick-add
 * menu's SlideInPanel.
 *
 * Mirrors the page-level transfer creation flow on
 * `frontend/app/transactions/page.tsx` (mode === "transfer"). The
 * existing inline form on that page stays for now, both surfaces post
 * to the same POST /api/v1/transactions/transfer endpoint so the
 * server is the single source of truth for the transfer-leg pair.
 *
 * Scope:
 *   - Transfer between two active accounts the caller owns.
 *   - Optional category override (defaults to the server's Transfer
 *     category when omitted).
 *   - No recurring (transfers are a discrete movement; recurring
 *     transfer templates aren't part of the L3.x Transfers contract).
 *
 * "Save & add new" behavior:
 *   - Default Save submits + closes the panel via onSaved().
 *   - "Save & add new" submits + clears the variable fields (amount,
 *     description, to_account), leaves the panel open, and refocuses
 *     the description input. Source account is preserved since most
 *     repeat-transfer flows are "from one account, multiple legs".
 *   - Both buttons route through the same form onSubmit so native HTML5
 *     validation runs before any network call.
 */

export interface TransferFormProps {
  accounts: Account[];
  categories: Category[];
  /** Pre-selected source account id (default-from-context). */
  defaultFromAccountId?: number | null;
  /** Called after a successful Save (panel-close path). */
  onSaved: () => void;
  /** Called after a category is created via the inline modal. */
  onCategoryCreated?: (category: Category) => void;
  /**
   * Called after every successful save (Save and Save & add new).
   * Pages can use this to refresh transaction lists.
   */
  onTransactionAdded?: () => void;
}

export default function TransferForm({
  accounts,
  categories,
  defaultFromAccountId = null,
  onSaved,
  onCategoryCreated,
  onTransactionAdded,
}: TransferFormProps) {
  const activeAccounts = accounts.filter((a) => a.is_active);
  const fallbackAccount =
    activeAccounts.find((a) => a.is_default) ?? activeAccounts[0];
  const initialFromAccountId =
    defaultFromAccountId ?? (fallbackAccount ? fallbackAccount.id : "");

  const [fromAccountId, setFromAccountId] = useState<number | "">(
    initialFromAccountId ?? "",
  );
  const [toAccountId, setToAccountId] = useState<number | "">("");
  const [categoryId, setCategoryId] = useState<number | "">("");
  const [description, setDescription] = useState("");
  const [amount, setAmount] = useState("");
  const [status, setStatus] = useState<"settled" | "pending">("settled");
  const [date, setDate] = useState(todayISO());
  const [submitting, setSubmitting] = useState(false);
  const [errMsg, setErrMsg] = useState("");
  const descRef = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const submitIntentRef = useRef<"save" | "save-and-add-new">("save");

  /**
   * Reset the submit-intent ref when native HTML5 validation rejects a
   * "Save and add new" attempt. Without this, the ref stays at
   * "save-and-add-new" and the user's next plain Save click would
   * silently keep the panel open + clear the form (wrong path). The
   * `invalid` event fires per-field, doesn't bubble by default, so we
   * attach it in the capture phase on the form element. requestSubmit()
   * runs validation synchronously and dispatches `invalid` to each
   * failed control before returning, so the reset happens before any
   * user-visible state can change.
   */
  useEffect(() => {
    const form = formRef.current;
    if (!form) return;
    const onInvalid = () => {
      submitIntentRef.current = "save";
    };
    form.addEventListener("invalid", onInvalid, true);
    return () => form.removeEventListener("invalid", onInvalid, true);
  }, []);

  // Pick up updated defaults if the parent supplies them after mount
  // (e.g. accounts loaded async). Mirrors TransactionForm's pattern.
  useEffect(() => {
    if (fromAccountId === "" && initialFromAccountId) {
      setFromAccountId(initialFromAccountId);
    }
    // Intentionally not reacting to subsequent activeAccounts changes,
    // we don't want to overwrite a user's manual selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFromAccountId]);

  /**
   * Source-change handler that clears the destination if it would collide
   * with the new source. The destination <select> filters its visible
   * options to exclude `fromAccountId`, but the controlled `toAccountId`
   * state is independent of that filter — without this guard a user can
   * pick source=A, destination=B, then switch source to B, leaving the
   * controlled state at toAccountId=B. The form would then POST
   * `from_account_id === to_account_id`, which the server rejects with a
   * 422 ("Source and destination accounts must be different",
   * backend/app/services/transaction_service.py:1389). Clearing client-
   * side keeps the failure surface at HTML5 required-field validation
   * (a clean re-pick) instead of a network round-trip.
   *
   * Mirrors handleAccountChange in app/transactions/page.tsx:816 — the
   * inline transfer form on /transactions has carried this clear since
   * the L3.x Transfers feature shipped.
   */
  function handleFromAccountChange(id: number | "") {
    setFromAccountId(id);
    if (toAccountId !== "" && id !== "" && toAccountId === id) {
      setToAccountId("");
    }
  }

  function clearForm() {
    // Keep source account (typical "from one account, multiple legs"
    // flow). Reset destination, amount, description, category override.
    setToAccountId("");
    setAmount("");
    setDescription("");
    setCategoryId("");
    setDate(todayISO());
    setErrMsg("");
  }

  async function submit(addAnother: boolean) {
    setSubmitting(true);
    setErrMsg("");
    try {
      await apiFetch("/api/v1/transactions/transfer", {
        method: "POST",
        body: JSON.stringify({
          from_account_id: fromAccountId,
          to_account_id: toAccountId,
          description,
          amount,
          status,
          date,
          ...(categoryId !== "" ? { category_id: categoryId } : {}),
        }),
      });
      onTransactionAdded?.();
      if (addAnother) {
        clearForm();
        window.setTimeout(() => descRef.current?.focus(), 0);
      } else {
        onSaved();
      }
    } catch (err) {
      setErrMsg(extractErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const addAnother = submitIntentRef.current === "save-and-add-new";
    submitIntentRef.current = "save";
    void submit(addAnother);
  }

  function handleSaveAndAddNewClick(e: MouseEvent<HTMLButtonElement>) {
    submitIntentRef.current = "save-and-add-new";
    const form = e.currentTarget.form;
    if (form) {
      form.requestSubmit();
    } else {
      submitIntentRef.current = "save";
    }
  }

  // Transfers need at least two active accounts. Categories are
  // optional (server defaults to Transfer), so we don't gate on them.
  if (activeAccounts.length < 2) {
    return (
      <div className="rounded-md border border-border bg-surface-raised p-4 text-sm text-text-secondary">
        Transfers move money between two accounts. Create a second active
        account before adding a transfer.
      </div>
    );
  }

  return (
    <form ref={formRef} onSubmit={onSubmit} className="space-y-4">
      {errMsg && <div className={errorCls}>{errMsg}</div>}

      <div>
        <label htmlFor="fab-xfer-from" className={label}>
          From account
        </label>
        <select
          id="fab-xfer-from"
          required
          value={fromAccountId}
          onChange={(e) =>
            handleFromAccountChange(
              e.target.value === "" ? "" : Number(e.target.value),
            )
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
        <label htmlFor="fab-xfer-to" className={label}>
          To account
        </label>
        <select
          id="fab-xfer-to"
          required
          value={toAccountId}
          onChange={(e) =>
            setToAccountId(
              e.target.value === "" ? "" : Number(e.target.value),
            )
          }
          className={input}
        >
          <option value="">Select account</option>
          {activeAccounts
            .filter((a) => a.id !== fromAccountId)
            .map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
        </select>
      </div>

      <div>
        <div className="flex items-center gap-1">
          <label htmlFor="fab-xfer-category" className={label}>
            Transfer category
          </label>
          <HelpAnchor
            section="transfers"
            label="Transfer category"
            variant="inline-title"
            className="mt-0"
          />
        </div>
        <CategorySelect
          id="fab-xfer-category"
          categories={categories}
          value={categoryId}
          onChange={setCategoryId}
          typeFilter="BOTH"
          className={input}
          onCategoryCreated={(cat) => onCategoryCreated?.(cat)}
        />
        <p className="mt-1 text-[10px] text-text-muted">
          Transfers use one category shared by both legs. Pick a
          transfer-compatible category like Transfer, Credit Card
          Payment, or Debt Repayment. Leave empty to use the default
          Transfer category.
        </p>
      </div>

      <div>
        <label htmlFor="fab-xfer-desc" className={label}>
          Description
        </label>
        <input
          id="fab-xfer-desc"
          ref={descRef}
          type="text"
          placeholder="Auto: Transfer from X to Y"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          className={input}
        />
      </div>

      <div>
        <label htmlFor="fab-xfer-amount" className={label}>
          Amount
        </label>
        <input
          id="fab-xfer-amount"
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
          <label htmlFor="fab-xfer-status" className={label}>
            Status
          </label>
          <select
            id="fab-xfer-status"
            value={status}
            onChange={(e) =>
              setStatus(e.target.value as "settled" | "pending")
            }
            className={input}
          >
            <option value="settled">Settled</option>
            <option value="pending">Pending</option>
          </select>
        </div>
        <div>
          <label htmlFor="fab-xfer-date" className={label}>
            Date
          </label>
          <input
            id="fab-xfer-date"
            type="date"
            required
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className={input}
          />
        </div>
      </div>

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
