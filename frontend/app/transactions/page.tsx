"use client";

import { FormEvent, Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { equalsAmount, formatAmount, formatLocalDate, toEditAmount, todayISO } from "@/lib/format";
import { input, label, btnPrimary, btnSecondary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import CategorySelect from "@/components/ui/CategorySelect";
import type { Account, Category, Transaction } from "@/lib/types";
import ConfirmModal from "@/components/ui/ConfirmModal";
import LinkAsTransferModal from "@/components/transactions/LinkAsTransferModal";
import MarkAsTransferModal from "@/components/transactions/MarkAsTransferModal";
import UnpairTransferModal from "@/components/transactions/UnpairTransferModal";



const PAGE_SIZE = 20;

// Column-aware sort defaults. When the user clicks a different column, that
// column's natural default direction is applied (Option B in the data-table
// pattern). Same-column clicks toggle direction. Numeric/date columns default
// to "desc" because most users want most-recent / largest-first.
type SortField =
  | "date"
  | "description"
  | "account_name"
  | "category_name"
  | "status"
  | "amount";

const SORT_DEFAULTS: Record<SortField, "asc" | "desc"> = {
  date: "desc",
  amount: "desc",
  description: "asc",
  account_name: "asc",
  category_name: "asc",
  status: "asc",
};

export default function TransactionsPage() {
  return (
    <Suspense fallback={
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    }>
      <TransactionsPageContent />
    </Suspense>
  );
}

function TransactionsPageContent() {
  const { user, loading } = useAuth();
  const searchParams = useSearchParams();
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  // Edit
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDesc, setEditDesc] = useState("");
  const [editAmount, setEditAmount] = useState("");
  const [editType, setEditType] = useState<"income" | "expense">("expense");
  const [editStatus, setEditStatus] = useState<"settled" | "pending">("settled");
  const [editDate, setEditDate] = useState("");
  // Expected settlement date for pending rows. Empty string means "not set"
  // and the field is only shown when editStatus === "pending". For settled
  // rows the backend stamps settled_date itself; surfacing it here would
  // confuse the spreadsheet/forecast model.
  const [editSettledDate, setEditSettledDate] = useState("");
  const [editAccountId, setEditAccountId] = useState<number | "">("");
  const [editCategoryId, setEditCategoryId] = useState<number | "">("");
  // Edit-time promote-to-recurring (L3.12). Hidden on rows that are already
  // recurring (a static chip is rendered instead). Default next_due_date is
  // "today + 30 days" so users get a reasonable starting point without a
  // backend round-trip.
  const [editPromoteRecurring, setEditPromoteRecurring] = useState(false);
  const [editRecFrequency, setEditRecFrequency] = useState<
    "weekly" | "biweekly" | "monthly" | "quarterly" | "yearly"
  >("monthly");
  const [editRecNextDue, setEditRecNextDue] = useState("");

  // Filters
  const [filterAccount, setFilterAccount] = useState<number | "">("");
  const [filterCategory, setFilterCategory] = useState<number | "">("");
  const [filterType, setFilterType] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [filterDateFrom, setFilterDateFrom] = useState("");
  const [filterDateTo, setFilterDateTo] = useState("");
  const [filterSearch, setFilterSearch] = useState("");
  const [sortField, setSortField] = useState<SortField>("date");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  // Billing periods for filter
  const [periods, setPeriods] = useState<{ id: number; start_date: string; end_date: string | null }[]>([]);
  const [filterPeriod, setFilterPeriod] = useState<string>("");

  // Form
  const [formMode, setFormMode] = useState<"transaction" | "transfer">("transaction");
  const [formAccountId, setFormAccountId] = useState<number | "">("");
  const [formToAccountId, setFormToAccountId] = useState<number | "">("");
  const [formTransferCatId, setFormTransferCatId] = useState<number | "">("");
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formDescription, setFormDescription] = useState("");
  const [formAmount, setFormAmount] = useState("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formStatus, setFormStatus] = useState<"settled" | "pending">("settled");
  const [formDate, setFormDate] = useState(todayISO());
  // Expected settlement date for pending creates. Left empty by default so
  // the user explicitly picks a settlement date when status=pending; this
  // keeps credit-card-style settlement lag a deliberate choice instead of
  // silently inheriting the transaction date.
  const [formSettledDate, setFormSettledDate] = useState("");
  const [formRecurring, setFormRecurring] = useState(false);
  const [formFrequency, setFormFrequency] = useState("monthly");
  const [formAutoSettle, setFormAutoSettle] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);

  // Transfer modals
  const [linkModalLegs, setLinkModalLegs] = useState<{ expense: Transaction; income: Transaction } | null>(null);
  const [markModalSource, setMarkModalSource] = useState<Transaction | null>(null);
  const [unpairModalLegs, setUnpairModalLegs] = useState<{ expense: Transaction; income: Transaction } | null>(null);
  // Partner row for the currently-edited linked transaction. Hydrated from
  // the visible list when possible, otherwise fetched on demand. Used to
  // filter the Account select and to render the mirror-amount notice.
  const [editPartner, setEditPartner] = useState<Transaction | null>(null);

  const loadRefs = useCallback(async () => {
    const [accts, cats, pers] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
      apiFetch<{ id: number; start_date: string; end_date: string | null }[]>("/api/v1/settings/billing-periods"),
    ]);
    setAccounts(accts ?? []);
    setCategories(cats ?? []);
    setPeriods(pers ?? []);
  }, []);

  const loadTransactions = useCallback(async (p: number) => {
    let url = `/api/v1/transactions?limit=${PAGE_SIZE + 1}&offset=${p * PAGE_SIZE}`;
    if (filterAccount) url += `&account_id=${filterAccount}`;
    if (filterCategory) url += `&category_id=${filterCategory}`;
    if (filterType) url += `&type=${filterType}`;
    if (filterStatus) url += `&status=${filterStatus}`;

    // Period filter overrides date_from/date_to
    if (filterPeriod) {
      const per = periods.find((p) => String(p.id) === filterPeriod);
      if (per) {
        url += `&date_from=${per.start_date}`;
        if (per.end_date) url += `&date_to=${per.end_date}`;
      }
    } else {
      if (filterDateFrom) url += `&date_from=${filterDateFrom}`;
      if (filterDateTo) url += `&date_to=${filterDateTo}`;
    }

    if (filterSearch) url += `&search=${encodeURIComponent(filterSearch)}`;
    const data = (await apiFetch<Transaction[]>(url)) ?? [];
    setHasMore(data.length > PAGE_SIZE);
    setTransactions(data.slice(0, PAGE_SIZE));
    setFetching(false);
  }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo, filterSearch, filterPeriod, periods]);

  useEffect(() => {
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  // Apply ?category= URL param once categories are loaded
  useEffect(() => {
    const categoryName = searchParams.get("category");
    if (categoryName && categories.length > 0) {
      const match = categories.find(
        (c) => c.name.toLowerCase() === categoryName.toLowerCase()
      );
      if (match) setFilterCategory(match.id);
    }
  }, [categories, searchParams]);

  useEffect(() => {
    if (!loading && user) {
      setFetching(true);
      loadTransactions(page).catch(() => setFetching(false));
    }
  }, [loading, user, loadTransactions, page]);

  useEffect(() => { setPage(0); }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo, filterSearch, filterPeriod]);

  useEffect(() => {
    clearSelection();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo, filterSearch, filterPeriod, sortField, sortDir, page]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (
        e.key === "Escape" &&
        selectedIds.size > 0 &&
        !confirmBulkDelete &&
        !bulkDeleting
      ) {
        clearSelection();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedIds.size, confirmBulkDelete, bulkDeleting]);

  function handleTypeChange(t: "income" | "expense") {
    setFormType(t);
    setFormCategoryId("");
  }

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    // Inline validation for the optional pending settled-date field. The
    // backend repeats the check, but matching the message client-side
    // keeps the form-submit experience snappy and mirror-consistent.
    if (
      formMode === "transaction" &&
      formStatus === "pending" &&
      formSettledDate &&
      formSettledDate < formDate
    ) {
      setError("Expected settlement date must be on or after the transaction date");
      return;
    }
    try {
      if (formMode === "transfer") {
        await apiFetch("/api/v1/transactions/transfer", {
          method: "POST",
          body: JSON.stringify({
            from_account_id: formAccountId,
            to_account_id: formToAccountId,
            description: formDescription,
            amount: formAmount,
            status: formStatus,
            date: formDate,
            ...(formTransferCatId !== "" ? { category_id: formTransferCatId } : {}),
          }),
        });
      } else {
        const created = await apiFetch<Transaction>("/api/v1/transactions", {
          method: "POST",
          body: JSON.stringify({
            account_id: formAccountId,
            category_id: formCategoryId,
            description: formDescription,
            amount: formAmount,
            type: formType,
            status: formStatus,
            date: formDate,
            // settled_date only travels on pending creates; SETTLED rows
            // get their settled_date stamped server-side from `date`.
            ...(formStatus === "pending" && formSettledDate
              ? { settled_date: formSettledDate }
              : {}),
          }),
        });
        // Promote the new tx to recurring if repeat is enabled. Using
        // promote-to-recurring (vs a separate POST /recurring) sets
        // tx.recurring_id on the source transaction so a subsequent edit
        // shows the "Recurring" chip — preventing the duplicate-template
        // bug where re-toggling "Make recurring" on edit would create a
        // second template because the source row stayed unlinked.
        if (formRecurring && formMode === "transaction" && created?.id) {
          // next_due_date must be today-or-later (server-side guard).
          // The Date input is already constrained to today via min=,
          // but defensively bump back-dated rows to today so the user's
          // tx still saves cleanly.
          const today = todayISO();
          const nextDue = formDate < today ? today : formDate;
          await apiFetch<Transaction>(
            `/api/v1/transactions/${created.id}/promote-to-recurring`,
            {
              method: "POST",
              body: JSON.stringify({
                frequency: formFrequency,
                next_due_date: nextDue,
                auto_settle: formAutoSettle,
              }),
            },
          );
        }
      }
      setFormDescription("");
      setFormAmount("");
      setFormType("expense");
      setFormStatus("settled");
      setFormToAccountId("");
      setFormTransferCatId("");
      setFormRecurring(false);
      setFormAutoSettle(false);
      setFormDate(todayISO());
      setFormSettledDate("");
      setShowForm(false);
      await loadTransactions(page);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  // Selection state operates on the VISIBLE rows only (transfer pairs are
  // rendered as a single row — the hidden half cascades server-side). Using
  // visibleTxs here keeps allPageSelected / togglePage consistent with what
  // the user actually sees.
  const selectionHiddenIds = new Set<number>();
  for (const t of transactions) {
    if (t.linked_transaction_id && t.id > t.linked_transaction_id) {
      selectionHiddenIds.add(t.id);
    }
  }
  const selectableTxs = transactions.filter((t) => !selectionHiddenIds.has(t.id));

  const allPageSelected =
    selectableTxs.length > 0 && selectableTxs.every((t) => selectedIds.has(t.id));
  const somePageSelected =
    selectableTxs.some((t) => selectedIds.has(t.id)) && !allPageSelected;

  function toggleOne(id: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function togglePage() {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (allPageSelected) {
        selectableTxs.forEach((t) => next.delete(t.id));
      } else {
        selectableTxs.forEach((t) => next.add(t.id));
      }
      return next;
    });
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  async function handleDelete(id: number) {
    setConfirmDeleteId(null);
    setError("");
    try {
      await apiFetch(`/api/v1/transactions/${id}`, { method: "DELETE" });
      await loadTransactions(page);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleBulkDelete() {
    setConfirmBulkDelete(false);
    setError("");
    setBulkDeleting(true);
    try {
      const body = { ids: Array.from(selectedIds) };
      const res = await apiFetch<{
        requested_count: number;
        deleted_count: number;
        skipped_ids: number[];
      }>("/api/v1/transactions/bulk-delete", {
        method: "POST",
        body: JSON.stringify(body),
      });
      clearSelection();
      await loadTransactions(page);
      if (res.skipped_ids.length > 0) {
        setError(
          `Deleted ${res.deleted_count} of ${res.requested_count} transactions. ${res.skipped_ids.length} ${res.skipped_ids.length === 1 ? "was" : "were"} already gone.`,
        );
      }
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setBulkDeleting(false);
    }
  }

  async function openUnpairModal(tx: Transaction) {
    if (!tx.linked_transaction_id) return;
    let partner: Transaction | null =
      transactions.find((t) => t.id === tx.linked_transaction_id) ?? null;
    if (!partner) {
      try {
        partner = (await apiFetch<Transaction>(`/api/v1/transactions/${tx.linked_transaction_id}`)) ?? null;
      } catch (err) {
        setError(extractErrorMessage(err));
        return;
      }
    }
    if (!partner) return;
    const expense = tx.type === "expense" ? tx : partner;
    const income = tx.type === "income" ? tx : partner;
    setUnpairModalLegs({ expense, income });
  }

  function defaultNextDueISO(): string {
    // 30 days out — gives users a reasonable starting due date without
    // surprising them with "today" when they tick the box.
    const d = new Date();
    d.setDate(d.getDate() + 30);
    return d.toISOString().slice(0, 10);
  }

  async function startEdit(tx: Transaction) {
    setEditingId(tx.id);
    setEditDesc(tx.description);
    setEditAmount(toEditAmount(tx.amount));
    setEditType(tx.type);
    setEditStatus(tx.status);
    setEditDate(tx.date);
    // Pre-fill from server settled_date if present (pending rows can carry
    // an "expected settlement date"); otherwise blank so the user can opt
    // in. SETTLED rows hide the field entirely so the existing value is
    // preserved server-side without the form touching it.
    setEditSettledDate(tx.status === "pending" && tx.settled_date ? tx.settled_date : "");
    setEditAccountId(tx.account_id);
    setEditCategoryId(tx.category_id);
    setEditPromoteRecurring(false);
    setEditRecFrequency("monthly");
    setEditRecNextDue(defaultNextDueISO());
    // Hydrate partner for linked rows so the Account select can filter
    // currency-compatible options and the mirror-amount notice can render.
    if (tx.linked_transaction_id) {
      const visible = transactions.find((t) => t.id === tx.linked_transaction_id);
      if (visible) {
        setEditPartner(visible);
      } else {
        try {
          const fetched = await apiFetch<Transaction>(`/api/v1/transactions/${tx.linked_transaction_id}`);
          setEditPartner(fetched ?? null);
        } catch {
          setEditPartner(null);
        }
      }
    } else {
      setEditPartner(null);
    }
  }

  function closeEdit() {
    setEditingId(null);
    setEditPartner(null);
    setEditPromoteRecurring(false);
  }

  async function handleSaveEdit() {
    if (editingId === null) return;
    if (!editDesc.trim()) { setError("Description is required"); return; }
    // Settled-date sanity check matches backend. Only enforced when the
    // user surfaced the field (pending status with a value entered).
    if (
      editStatus === "pending" &&
      editSettledDate &&
      editSettledDate < editDate
    ) {
      setError("Expected settlement date must be on or after the transaction date");
      return;
    }
    setError("");
    // Capture the row pre-save so we can decide whether the promote step
    // applies (transfer legs and already-recurring rows are excluded).
    const editingRow = transactions.find((t) => t.id === editingId) ?? null;
    const wantsPromote =
      editPromoteRecurring &&
      editingRow !== null &&
      editingRow.linked_transaction_id === null &&
      editingRow.recurring_id === null;
    if (wantsPromote && !editRecNextDue) {
      setError("Pick a next due date");
      return;
    }
    if (wantsPromote && editRecNextDue < todayISO()) {
      setError("Date must be today or later");
      return;
    }
    try {
      const isLinked = editPartner !== null;
      const body: Record<string, unknown> = {
        description: editDesc,
        amount: editAmount,
        status: editStatus,
        date: editDate,
        account_id: editAccountId,
        category_id: editCategoryId,
      };
      if (!isLinked) {
        body.type = editType;
      }
      // Send settled_date only on pending edits. Settled rows keep their
      // existing settled_date untouched (the backend stamps it from the
      // transition); piggy-backing the field would risk overwriting the
      // server's authoritative value.
      if (editStatus === "pending") {
        body.settled_date = editSettledDate || null;
      }
      await apiFetch(`/api/v1/transactions/${editingId}`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      if (wantsPromote) {
        // The PUT already committed the edit. If the promote step then
        // fails, surface a partial-success message so the user knows
        // the transaction edits stuck even though the combined action
        // reported an error.
        try {
          const promoted = await apiFetch<Transaction>(
            `/api/v1/transactions/${editingId}/promote-to-recurring`,
            {
              method: "POST",
              body: JSON.stringify({
                frequency: editRecFrequency,
                next_due_date: editRecNextDue,
              }),
            },
          );
          // Optimistically reflect the new recurring_id locally so the chip
          // appears immediately even before loadTransactions resolves. Only
          // patch when the response actually includes a non-null recurring_id
          // — if the body is missing or malformed, fall through to the
          // loadTransactions(page) refetch below so the row reconciles to
          // server truth instead of staying optimistically wrong.
          if (promoted && promoted.recurring_id != null) {
            setTransactions((prev) =>
              prev.map((t) =>
                t.id === editingId
                  ? { ...t, recurring_id: promoted.recurring_id }
                  : t,
              ),
            );
          }
        } catch (promoteErr) {
          const reason = extractErrorMessage(promoteErr);
          setError(
            `Transaction updated, but promote-to-recurring failed: ${reason}. The transaction still reflects your edits.`,
          );
          // Exit edit mode (the edit DID persist) and refresh so the row
          // shows the saved values; the error banner stays visible.
          closeEdit();
          await loadTransactions(page);
          return;
        }
      }
      closeEdit();
      await loadTransactions(page);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleToggleStatus(tx: Transaction) {
    setError("");
    try {
      await apiFetch(`/api/v1/transactions/${tx.id}`, {
        method: "PUT",
        body: JSON.stringify({ status: tx.status === "settled" ? "pending" : "settled" }),
      });
      await loadTransactions(page);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  const activeAccounts = accounts.filter((a) => a.is_active);

  // Sort helper. Same-column click toggles direction. Different-column click
  // applies that column's natural default (see SORT_DEFAULTS above) so users
  // get a sensible starting state instead of always-asc, which felt like
  // their previous direction was "dropped".
  function toggleSort(field: SortField) {
    if (sortField === field) {
      setSortDir((prev) => (prev === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir(SORT_DEFAULTS[field]);
    }
  }
  const sortedTransactions = [...transactions].sort((a, b) => {
    let cmp = 0;
    if (sortField === "date") cmp = a.date.localeCompare(b.date);
    else if (sortField === "description") cmp = a.description.localeCompare(b.description);
    else if (sortField === "account_name") cmp = a.account_name.localeCompare(b.account_name);
    else if (sortField === "category_name") cmp = a.category_name.localeCompare(b.category_name);
    else if (sortField === "status") cmp = a.status.localeCompare(b.status);
    else if (sortField === "amount") cmp = Number(a.amount) - Number(b.amount);
    return sortDir === "asc" ? cmp : -cmp;
  });
  const defaultAccount = activeAccounts.find((a) => a.is_default);

  // Pre-select default account when opening form
  useEffect(() => {
    if (showForm && formAccountId === "" && defaultAccount) {
      setFormAccountId(defaultAccount.id);
      if (defaultAccount.account_type_slug === "credit_card") setFormStatus("pending");
    }
  }, [showForm, formAccountId, defaultAccount]);

  function handleAccountChange(id: number | "") {
    setFormAccountId(id);
    if (formToAccountId === id) setFormToAccountId("");
    const acct = accounts.find((a) => a.id === id);
    setFormStatus(acct?.account_type_slug === "credit_card" ? "pending" : "settled");
  }

  // Bulk "Link as transfer" validation. Server is the source of truth;
  // this is advisory only so we can disable the button + show a tooltip.
  function evaluateLinkSelection(): {
    visible: boolean;
    enabled: boolean;
    reason: string | null;
    expense: Transaction | null;
    income: Transaction | null;
  } {
    const ids = Array.from(selectedIds);
    if (ids.length !== 2) {
      return { visible: false, enabled: false, reason: null, expense: null, income: null };
    }
    const rows = ids
      .map((id) => transactions.find((t) => t.id === id))
      .filter((t): t is Transaction => Boolean(t));
    if (rows.length !== 2) {
      return { visible: false, enabled: false, reason: null, expense: null, income: null };
    }
    const [a, b] = rows;
    if (a.linked_transaction_id !== null || b.linked_transaction_id !== null) {
      return { visible: false, enabled: false, reason: null, expense: null, income: null };
    }
    // 2 un-linked rows → button is visible from here on; enabled depends on rules.
    if (a.type === b.type) {
      const reason =
        a.type === "expense"
          ? "Both selected rows are expenses"
          : "Both selected rows are incomes";
      return { visible: true, enabled: false, reason, expense: null, income: null };
    }
    if (!equalsAmount(String(a.amount), String(b.amount))) {
      return { visible: true, enabled: false, reason: "Amounts differ", expense: null, income: null };
    }
    if (a.account_id === b.account_id) {
      return { visible: true, enabled: false, reason: "Same account", expense: null, income: null };
    }
    const acctA = accounts.find((x) => x.id === a.account_id);
    const acctB = accounts.find((x) => x.id === b.account_id);
    if (!acctA || !acctB) {
      return { visible: true, enabled: false, reason: "Account not found", expense: null, income: null };
    }
    if (acctA.currency !== acctB.currency) {
      return { visible: true, enabled: false, reason: "Different currencies", expense: null, income: null };
    }
    const expense = a.type === "expense" ? a : b;
    const income = a.type === "income" ? a : b;
    return { visible: true, enabled: true, reason: null, expense, income };
  }

  const linkSelection = evaluateLinkSelection();

  return (
    <AppShell>
      {selectedIds.size > 0 && (
        <div className="sticky top-0 z-20 -mx-4 sm:-mx-8 mb-4 flex items-center justify-between gap-3 border-b border-border bg-surface-raised/95 px-4 sm:px-8 py-3 backdrop-blur">
          <span className="text-sm font-medium" aria-live="polite">
            {selectedIds.size} selected
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className={btnSecondary}
              onClick={clearSelection}
              disabled={bulkDeleting}
            >
              Clear
            </button>
            {linkSelection.visible && (
              <button
                type="button"
                className={btnSecondary}
                title={linkSelection.reason ?? "Link the two selected rows as a transfer"}
                disabled={!linkSelection.enabled || bulkDeleting}
                onClick={() => {
                  if (linkSelection.enabled && linkSelection.expense && linkSelection.income) {
                    setLinkModalLegs({ expense: linkSelection.expense, income: linkSelection.income });
                  }
                }}
              >
                Link as transfer
              </button>
            )}
            <button
              type="button"
              className="inline-flex min-h-[44px] items-center rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
              onClick={() => setConfirmBulkDelete(true)}
              disabled={bulkDeleting}
            >
              {bulkDeleting ? "Deleting…" : "Delete selected"}
            </button>
          </div>
        </div>
      )}
      <div className="mb-8 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Transactions</h1>
        <div className="flex items-center gap-2">
          {activeAccounts.length > 0 && categories.length > 0 && (
            <button onClick={() => setShowForm(!showForm)} className={btnPrimary}>
              {showForm ? "Cancel" : "+ New Transaction"}
            </button>
          )}
          <Link href="/import" className={btnSecondary}>
            Import
          </Link>
        </div>
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {showForm && (
        <div className={`mb-6 ${card} p-6`}>
          <div className="mb-4 flex items-center gap-4">
            <h2 className={cardTitle}>{formMode === "transfer" ? "New Transfer" : "New Transaction"}</h2>
            <div className="flex rounded-md border border-border text-xs">
              <button type="button" onClick={() => setFormMode("transaction")} className={`px-3 py-1 rounded-l-md ${formMode === "transaction" ? "bg-accent text-accent-text" : "text-text-muted hover:bg-surface-raised"}`}>Transaction</button>
              <button type="button" onClick={() => setFormMode("transfer")} className={`px-3 py-1 rounded-r-md ${formMode === "transfer" ? "bg-accent text-accent-text" : "text-text-muted hover:bg-surface-raised"}`}>Transfer</button>
            </div>
          </div>
          <form onSubmit={handleAdd} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <label htmlFor="tx-account" className={label}>{formMode === "transfer" ? "From Account" : "Account"}</label>
              <select id="tx-account" required value={formAccountId} onChange={(e) => handleAccountChange(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                <option value="">Select account</option>
                {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </div>
            {formMode === "transfer" ? (
              <div>
                <label htmlFor="tx-to-account" className={label}>To Account</label>
                <select id="tx-to-account" required value={formToAccountId} onChange={(e) => setFormToAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                  <option value="">Select account</option>
                  {activeAccounts.filter((a) => a.id !== formAccountId).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              </div>
            ) : (
              <div>
                <label htmlFor="tx-type" className={label}>Type</label>
                <select id="tx-type" value={formType} onChange={(e) => handleTypeChange(e.target.value as "income" | "expense")} className={input}>
                  <option value="expense">Expense</option>
                  <option value="income">Income</option>
                </select>
              </div>
            )}
            {formMode === "transaction" && (
              <div>
                <label htmlFor="tx-category" className={label}>Category</label>
                <CategorySelect id="tx-category" categories={categories} value={formCategoryId} onChange={setFormCategoryId} filterType={formType} className={input} onCategoryCreated={(cat) => setCategories((prev) => [...prev, cat])} />
              </div>
            )}
            {formMode === "transfer" && (
              <div>
                <label className={label}>Category (optional)</label>
                <CategorySelect id="tx-transfer-cat" categories={categories} value={formTransferCatId} onChange={setFormTransferCatId} className={input} onCategoryCreated={(cat) => setCategories((prev) => [...prev, cat])} />
                <p className="mt-1 text-[10px] text-text-muted">Defaults to Transfer. Override to track in budgets.</p>
              </div>
            )}
            <div>
              <label htmlFor="tx-desc" className={label}>Description</label>
              <input id="tx-desc" type="text" required={formMode === "transaction"} placeholder={formMode === "transfer" ? "Auto: Transfer from X to Y" : "What was it for?"} value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="tx-amount" className={label}>Amount</label>
              <input id="tx-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="tx-status" className={label}>Status</label>
              <select id="tx-status" value={formStatus} onChange={(e) => setFormStatus(e.target.value as "settled" | "pending")} className={input}>
                <option value="settled">Settled</option>
                <option value="pending">Pending</option>
              </select>
            </div>
            <div>
              <label htmlFor="tx-date" className={label}>Date</label>
              <input id="tx-date" type="date" required value={formDate} onChange={(e) => setFormDate(e.target.value)} className={input} />
            </div>
            {formMode === "transaction" && formStatus === "pending" && (
              <div>
                <label htmlFor="tx-settled-date" className={label}>
                  Expected settlement date
                </label>
                <input
                  id="tx-settled-date"
                  type="date"
                  min={formDate}
                  value={formSettledDate}
                  onChange={(e) => setFormSettledDate(e.target.value)}
                  className={input}
                />
                <p className="mt-1 text-[10px] text-text-muted">
                  Optional. When the bank actually charges the card.
                </p>
              </div>
            )}
            {formMode === "transaction" && (
              <div className="flex items-end gap-4">
                <label className="flex items-center gap-2 text-sm text-text-secondary">
                  <input type="checkbox" checked={formRecurring} onChange={(e) => setFormRecurring(e.target.checked)} className="rounded border-border" />
                  Repeats
                </label>
                {formRecurring && (
                  <>
                    <div>
                      <label htmlFor="tx-freq" className="sr-only">Frequency</label>
                      <select id="tx-freq" value={formFrequency} onChange={(e) => setFormFrequency(e.target.value)} className={input}>
                        <option value="weekly">Weekly</option>
                        <option value="biweekly">Every 2 weeks</option>
                        <option value="monthly">Monthly</option>
                        <option value="quarterly">Quarterly</option>
                        <option value="yearly">Yearly</option>
                      </select>
                    </div>
                    <label className="flex items-center gap-2 text-xs text-text-muted">
                      <input type="checkbox" checked={formAutoSettle} onChange={(e) => setFormAutoSettle(e.target.checked)} className="rounded border-border" />
                      Auto-settle
                    </label>
                  </>
                )}
              </div>
            )}
            <div className="flex items-end">
              <button type="submit" className={btnPrimary}>{formMode === "transfer" ? "Transfer" : "Add Transaction"}</button>
            </div>
          </form>
        </div>
      )}

      {/* Search + Preset filters */}
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center sm:gap-3">
        <div className="w-full sm:flex-1 sm:min-w-[200px]">
          <label htmlFor="f-search" className="sr-only">Search transactions</label>
          <input id="f-search" type="text" placeholder="Search descriptions..." value={filterSearch} onChange={(e) => setFilterSearch(e.target.value)} className={input} />
        </div>
        <div className="flex flex-wrap gap-1">
          {(() => {
            // Quick-filter buttons. Each clears `filterPeriod` first because the
            // period filter overrides date_from/date_to in the URL builder, so
            // leaving it set would silently make the click a no-op.
            const setRange = (from: string, to: string) => {
              setFilterPeriod("");
              setFilterDateFrom(from);
              setFilterDateTo(to);
            };
            const presets: { label: string; fn: () => void }[] = [
              {
                label: "Today",
                fn: () => {
                  const d = todayISO();
                  setRange(d, d);
                },
              },
              {
                label: "This Week",
                fn: () => {
                  const now = new Date();
                  const day = now.getDay();
                  const diff = day === 0 ? 6 : day - 1; // Monday = start of week
                  const mon = new Date(now);
                  mon.setDate(now.getDate() - diff);
                  setRange(formatLocalDate(mon), todayISO());
                },
              },
              {
                label: "This Month",
                fn: () => {
                  const now = new Date();
                  setRange(
                    formatLocalDate(new Date(now.getFullYear(), now.getMonth(), 1)),
                    todayISO(),
                  );
                },
              },
              {
                label: "All",
                fn: () => setRange("", ""),
              },
            ];
            // Optional "Current Period" preset, only when an open billing
            // period exists. It sets the period filter (which the URL builder
            // already prefers) and clears any explicit date range.
            const currentPeriod = periods.find((p) => p.end_date === null);
            if (currentPeriod) {
              presets.push({
                label: "Current Period",
                fn: () => {
                  setFilterDateFrom("");
                  setFilterDateTo("");
                  setFilterPeriod(String(currentPeriod.id));
                },
              });
            }
            return presets.map((p) => (
              <button key={p.label} type="button" onClick={p.fn} className="rounded-md border border-border px-2.5 py-1 text-[11px] text-text-secondary hover:bg-surface-raised min-h-[44px] sm:min-h-0">
                {p.label}
              </button>
            ));
          })()}
        </div>
      </div>
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:gap-3">
        <div className="w-full sm:w-auto">
          <label htmlFor="f-account" className="sr-only">Filter by account</label>
          <select id="f-account" value={filterAccount} onChange={(e) => setFilterAccount(e.target.value === "" ? "" : Number(e.target.value))} className={`w-full sm:w-40 ${input}`}>
            <option value="">All accounts</option>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
        <div className="w-full sm:w-auto">
          <label htmlFor="f-category" className="sr-only">Filter by category</label>
          <select id="f-category" value={filterCategory} onChange={(e) => setFilterCategory(e.target.value === "" ? "" : Number(e.target.value))} className={`w-full sm:w-40 ${input}`}>
            <option value="">All categories</option>
            {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
        <div className="w-full sm:w-auto">
          <label htmlFor="f-type" className="sr-only">Filter by type</label>
          <select id="f-type" value={filterType} onChange={(e) => setFilterType(e.target.value)} className={`w-full sm:w-32 ${input}`}>
            <option value="">All types</option>
            <option value="income">Income</option>
            <option value="expense">Expense</option>
          </select>
        </div>
        <div className="w-full sm:w-auto">
          <label htmlFor="f-status" className="sr-only">Filter by status</label>
          <select id="f-status" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)} className={`w-full sm:w-32 ${input}`}>
            <option value="">All statuses</option>
            <option value="settled">Settled</option>
            <option value="pending">Pending</option>
          </select>
        </div>
        <div className="w-full sm:w-auto">
          <label htmlFor="f-from" className="sr-only">From date</label>
          <input id="f-from" type="date" value={filterDateFrom} onChange={(e) => { setFilterPeriod(""); setFilterDateFrom(e.target.value); }} className={`w-full sm:w-32 ${input}`} placeholder="From" />
        </div>
        <div className="w-full sm:w-auto">
          <label htmlFor="f-to" className="sr-only">To date</label>
          <input id="f-to" type="date" value={filterDateTo} onChange={(e) => { setFilterPeriod(""); setFilterDateTo(e.target.value); }} className={`w-full sm:w-32 ${input}`} placeholder="To" />
        </div>
        {periods.length > 0 && (
          <div className="w-full sm:w-auto">
            <label htmlFor="f-period" className="sr-only">Billing period</label>
            <select id="f-period" value={filterPeriod} onChange={(e) => { setFilterPeriod(e.target.value); if (e.target.value) { setFilterDateFrom(""); setFilterDateTo(""); } }} className={`w-full sm:w-40 ${input}`}>
              <option value="">All periods</option>
              {periods.map((p) => (
                <option key={p.id} value={String(p.id)}>
                  {p.start_date}{p.end_date ? ` — ${p.end_date}` : " (current)"}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {fetching ? (
        <Spinner />
      ) : (
        <>
          <div className={`${card} md:overflow-x-auto`}>
            <div className="hidden md:block border-b border-border px-6 py-3">
              <div className="grid grid-cols-12 gap-4 text-xs font-medium uppercase tracking-wider text-text-muted">
                <div className="col-span-1 flex items-center">
                  <input
                    type="checkbox"
                    aria-label="Select all on page"
                    checked={allPageSelected}
                    ref={(el) => {
                      if (el) el.indeterminate = somePageSelected;
                    }}
                    onChange={togglePage}
                    className="h-4 w-4"
                  />
                </div>
                {([
                  { field: "date" as const, label: "Date", span: "col-span-2", align: "" },
                  { field: "description" as const, label: "Description", span: "col-span-2", align: "" },
                  { field: "account_name" as const, label: "Account", span: "col-span-2", align: "" },
                  { field: "category_name" as const, label: "Category", span: "col-span-1", align: "" },
                  { field: "status" as const, label: "Status", span: "col-span-1", align: "text-center" },
                  { field: "amount" as const, label: "Amount", span: "col-span-1", align: "text-right" },
                ]).map((col) => (
                  <button key={col.field} onClick={() => toggleSort(col.field)} className={`${col.span} ${col.align} min-h-[32px] hover:text-text-primary transition-colors`}>
                    {col.label}{sortField === col.field ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                  </button>
                ))}
                <span className="col-span-2" />
              </div>
            </div>
            {(() => {
              // Precompute tx map for O(1) lookups. The dedupe set is hoisted
              // above (selectionHiddenIds) so the selection helpers see the
              // same hidden-half rule as the render.
              const txMap = new Map(transactions.map((t) => [t.id, t]));
              const visibleTxs = sortedTransactions.filter((t) => !selectionHiddenIds.has(t.id));
              return (
                <>
                  {/* Desktop/tablet grid rows (md+) */}
                  <div className="hidden md:block divide-y divide-border-subtle">
                    {visibleTxs.map((tx) => {
                      const isTransfer = tx.linked_transaction_id !== null;
                      const linkedTx = isTransfer ? txMap.get(tx.linked_transaction_id!) : null;
                      return editingId === tx.id ? (
                        // Desktop edit mode: switched from a single 12-col row
                        // (Item 7 audit: Status/Amount cols ~42px clipped both
                        // the select label and the type/amount split) to a
                        // labeled stacked form. Fields lay out 4-up so each
                        // input gets ~22% of the row width, wide enough for
                        // the descriptive option labels ("Settled"/"Pending",
                        // "Expense"/"Income") that previously had to be hidden
                        // behind a !w-14 override.
                        <div
                          key={tx.id}
                          className="bg-surface-raised px-6 py-4"
                          data-testid={`edit-row-desktop-${tx.id}`}
                        >
                          {editPartner && (
                            <div className="mb-3 text-xs text-accent" data-testid={`edit-mirror-notice-${tx.id}`}>
                              Editing a transfer leg. Changes to amount apply to both rows.
                            </div>
                          )}
                          <div className="flex items-center gap-3 mb-3">
                            <input
                              type="checkbox"
                              aria-label={`Select transaction ${tx.id}`}
                              checked={selectedIds.has(tx.id)}
                              onChange={() => toggleOne(tx.id)}
                              className="h-4 w-4"
                            />
                            <span className="text-xs uppercase tracking-wider text-text-muted">
                              Editing transaction
                            </span>
                          </div>
                          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                            <div>
                              <label htmlFor={`edit-date-${tx.id}`} className={label}>Date</label>
                              <input id={`edit-date-${tx.id}`} aria-label="Date" type="date" value={editDate} onChange={(e) => setEditDate(e.target.value)} className={`text-sm ${input}`} />
                            </div>
                            <div className="lg:col-span-2">
                              <label htmlFor={`edit-desc-${tx.id}`} className={label}>Description</label>
                              <input id={`edit-desc-${tx.id}`} aria-label="Description" type="text" required value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className={`text-sm ${input}`} />
                            </div>
                            <div>
                              <label htmlFor={`edit-account-${tx.id}`} className={label}>Account</label>
                              <select
                                id={`edit-account-${tx.id}`}
                                aria-label="Account"
                                value={editAccountId}
                                onChange={(e) => setEditAccountId(e.target.value === "" ? "" : Number(e.target.value))}
                                className={`text-sm ${input}`}
                              >
                                {accounts
                                  .filter((a) => {
                                    if (!editPartner) return true;
                                    if (a.id === editPartner.account_id) return false;
                                    const partnerAcct = accounts.find((x) => x.id === editPartner.account_id);
                                    return partnerAcct ? a.currency === partnerAcct.currency : true;
                                  })
                                  .map((a) => <option key={a.id} value={a.id}>{a.name}{!a.is_active ? " (inactive)" : ""}</option>)}
                              </select>
                            </div>
                            <div>
                              <label htmlFor={`edit-cat-${tx.id}`} className={label}>Category</label>
                              <CategorySelect aria-label="Category" id={`edit-cat-${tx.id}`} categories={categories} value={editCategoryId} onChange={setEditCategoryId} filterType={editType} className={`text-sm ${input}`} onCategoryCreated={(cat) => setCategories((prev) => [...prev, cat])} />
                            </div>
                            <div>
                              <label htmlFor={`edit-status-${tx.id}`} className={label}>Status</label>
                              <select id={`edit-status-${tx.id}`} aria-label="Status" value={editStatus} onChange={(e) => setEditStatus(e.target.value as "settled" | "pending")} className={`text-sm ${input}`}>
                                <option value="settled">Settled</option>
                                <option value="pending">Pending</option>
                              </select>
                            </div>
                            <div>
                              <label htmlFor={`edit-type-${tx.id}`} className={label}>Type</label>
                              {editPartner ? (
                                <span
                                  id={`edit-type-${tx.id}`}
                                  aria-label="Type"
                                  title="Type is fixed for transfer legs."
                                  className="text-sm flex items-center px-3 rounded border border-border bg-surface text-text-muted h-10"
                                >
                                  {editType === "expense" ? "Expense" : "Income"}
                                </span>
                              ) : (
                                <select id={`edit-type-${tx.id}`} aria-label="Type" value={editType} onChange={(e) => { setEditType(e.target.value as "income" | "expense"); setEditCategoryId(""); }} className={`text-sm ${input}`}>
                                  <option value="expense">Expense</option>
                                  <option value="income">Income</option>
                                </select>
                              )}
                            </div>
                            <div>
                              <label htmlFor={`edit-amount-${tx.id}`} className={label}>Amount</label>
                              <input id={`edit-amount-${tx.id}`} aria-label="Amount" type="number" step="0.01" min="0.01" value={editAmount} onChange={(e) => setEditAmount(e.target.value)} className={`text-sm ${input}`} />
                            </div>
                            {editStatus === "pending" && (
                              <div data-testid={`edit-settled-date-cell-${tx.id}`}>
                                <label htmlFor={`edit-settled-${tx.id}`} className={label}>
                                  Expected settlement
                                </label>
                                <input
                                  id={`edit-settled-${tx.id}`}
                                  aria-label="Expected settlement date"
                                  type="date"
                                  min={editDate}
                                  value={editSettledDate}
                                  onChange={(e) => setEditSettledDate(e.target.value)}
                                  className={`text-sm ${input}`}
                                />
                              </div>
                            )}
                          </div>
                          {/* Promote-to-recurring (L3.12). Hidden for transfer legs;
                              shown as a static chip when the row is already recurring. */}
                          {!editPartner && (
                            <div className="mt-3" data-testid={`edit-recurring-row-${tx.id}`}>
                              {tx.recurring_id !== null ? (
                                <span
                                  className="inline-flex items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] text-text-muted"
                                  data-testid={`edit-recurring-chip-${tx.id}`}
                                >
                                  Recurring
                                </span>
                              ) : (
                                <div className="flex flex-wrap items-center gap-3">
                                  <label className="inline-flex items-center gap-2 text-xs text-text-secondary">
                                    <input
                                      type="checkbox"
                                      aria-label="Make recurring"
                                      checked={editPromoteRecurring}
                                      onChange={(e) => setEditPromoteRecurring(e.target.checked)}
                                      className="h-4 w-4"
                                      data-testid={`edit-recurring-toggle-${tx.id}`}
                                    />
                                    Make recurring
                                  </label>
                                  {editPromoteRecurring && (
                                    <>
                                      <select
                                        aria-label="Frequency"
                                        value={editRecFrequency}
                                        onChange={(e) =>
                                          setEditRecFrequency(
                                            e.target.value as typeof editRecFrequency,
                                          )
                                        }
                                        className={`text-[11px] !w-32 ${input}`}
                                      >
                                        <option value="weekly">Weekly</option>
                                        <option value="biweekly">Biweekly</option>
                                        <option value="monthly">Monthly</option>
                                        <option value="quarterly">Quarterly</option>
                                        <option value="yearly">Yearly</option>
                                      </select>
                                      <input
                                        aria-label="Next due date"
                                        type="date"
                                        min={todayISO()}
                                        value={editRecNextDue}
                                        onChange={(e) => setEditRecNextDue(e.target.value)}
                                        className={`text-[11px] !w-40 ${input}`}
                                      />
                                    </>
                                  )}
                                </div>
                              )}
                            </div>
                          )}
                          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border-subtle pt-3">
                            {/* 44px touch-target floor matches the mobile edit
                                form and the project a11y baseline (per PRs
                                #173/#174). md+ tablet width also lands here, so
                                36px would land below WCAG 2.5.8 AA (24px) and
                                comfortably below the project's stricter floor. */}
                            <button onClick={handleSaveEdit} className="min-h-[44px] rounded-md bg-accent px-4 text-sm font-medium text-accent-text hover:bg-accent-hover">Save</button>
                            <button onClick={closeEdit} className="min-h-[44px] rounded-md border border-border px-4 text-sm text-text-secondary hover:bg-surface-raised">Cancel</button>
                          </div>
                        </div>
                      ) : (
                        <div
                          key={tx.id}
                          className={`grid grid-cols-12 items-center gap-4 px-6 py-3 transition-colors hover:bg-surface-raised ${
                            tx.status === "pending"
                              ? "[&>*:not(.tx-status-cell)]:opacity-60"
                              : ""
                          }`}
                        >
                          {/* Pending rows dim every cell except the status pill
                              via the [&>*:not(.tx-status-cell)] selector above.
                              CSS opacity composites with ancestor opacity, so a
                              naive `opacity-60` on the parent + `opacity-100` on
                              the pill would still paint the pill at 60% (60×100).
                              Splitting per-child preserves the pill's vivid amber
                              while keeping the rest of the row dimmed. */}
                          <span className="col-span-1 flex items-center">
                            <input
                              type="checkbox"
                              aria-label={`Select transaction ${tx.id}`}
                              checked={selectedIds.has(tx.id)}
                              onChange={() => toggleOne(tx.id)}
                              className="h-4 w-4"
                            />
                          </span>
                          <span className="col-span-2 text-sm tabular-nums text-text-secondary">
                            {tx.date}
                            {/* Expected settlement subtext (Item 13). Surfaces
                                only when the row is pending AND the user set a
                                custom settlement date that differs from the
                                transaction date. Otherwise the subtext would
                                be redundant noise. */}
                            {tx.status === "pending" && tx.settled_date && tx.settled_date !== tx.date && (
                              <span
                                className="block text-[10px] text-text-muted"
                                data-testid={`expected-settled-${tx.id}`}
                              >
                                expected settled {tx.settled_date}
                              </span>
                            )}
                          </span>
                          <span className="col-span-2 text-sm text-text-primary">{tx.description}</span>
                          <span className="col-span-2 text-sm text-text-secondary truncate">
                            {isTransfer && linkedTx
                              ? <>{tx.account_name} &rarr; {linkedTx.account_name}</>
                              : tx.account_name}
                          </span>
                          <span className="col-span-1 text-sm text-text-secondary truncate">{tx.category_name}</span>
                          <span className="tx-status-cell col-span-1 text-center">
                            {isTransfer ? (
                              <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-warning-dim text-warning"}`}>
                                {tx.status}
                              </span>
                            ) : (
                              <button
                                onClick={() => handleToggleStatus(tx)}
                                aria-label={`Mark as ${tx.status === "settled" ? "pending" : "settled"}`}
                                className="inline-flex min-h-[44px] items-center justify-center"
                              >
                                {/* Outer button = WCAG 2.5.8 hit area;
                                    inner span = lean pill visual. */}
                                <span
                                  className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                                    tx.status === "settled"
                                      ? "bg-success-dim text-success"
                                      : "bg-warning-dim text-warning"
                                  }`}
                                >
                                  {tx.status}
                                </span>
                              </button>
                            )}
                          </span>
                          <span className={`col-span-1 text-right text-sm font-medium tabular-nums ${isTransfer ? "text-accent" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                            {isTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                          </span>
                          <span className="col-span-2 flex flex-wrap justify-end gap-x-2 gap-y-1">
                            <button onClick={() => startEdit(tx)} aria-label={`Edit: ${tx.description}`} disabled={bulkDeleting} className="whitespace-nowrap text-xs text-text-muted hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed">Edit</button>
                            {!isTransfer && (
                              <button onClick={() => setMarkModalSource(tx)} aria-label={`Mark as transfer: ${tx.description}`} disabled={bulkDeleting} className="whitespace-nowrap text-xs text-text-muted hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed">Mark transfer</button>
                            )}
                            {isTransfer && (
                              <button onClick={() => openUnpairModal(tx)} aria-label={`Unlink transfer: ${tx.description}`} disabled={bulkDeleting} className="whitespace-nowrap text-xs text-text-muted hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed">Unlink</button>
                            )}
                            <button onClick={() => setConfirmDeleteId(tx.id)} aria-label={`Delete: ${tx.description}`} disabled={bulkDeleting} className="whitespace-nowrap text-xs text-text-muted hover:text-danger disabled:opacity-40 disabled:cursor-not-allowed">Delete</button>
                          </span>
                        </div>
                      );
                    })}
                    {visibleTxs.length === 0 && (
                      <div className="px-6 py-8 text-center text-sm text-text-muted">
                        {activeAccounts.length === 0
                          ? "Create an account first."
                          : categories.length === 0
                            ? "Create a category first."
                            : "No transactions match your filters."}
                      </div>
                    )}
                  </div>

                  {/* Mobile card layout (below md) */}
                  <div className="md:hidden flex flex-col gap-3 p-3">
                    {visibleTxs.map((tx) => {
                      const isTransfer = tx.linked_transaction_id !== null;
                      const linkedTx = isTransfer ? txMap.get(tx.linked_transaction_id!) : null;
                      if (editingId === tx.id) {
                        return (
                          <article key={tx.id} className="flex flex-col gap-3 rounded-lg border border-border bg-surface-raised p-4 shadow-sm">
                            {editPartner && (
                              <div className="text-xs text-accent" data-testid={`edit-mirror-notice-mobile-${tx.id}`}>
                                Editing a transfer leg. Changes to amount apply to both rows.
                              </div>
                            )}
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                              <div>
                                <label className={label}>Date</label>
                                <input aria-label="Date" type="date" value={editDate} onChange={(e) => setEditDate(e.target.value)} className={`text-sm ${input}`} />
                              </div>
                              <div>
                                <label className={label}>Description</label>
                                <input aria-label="Description" type="text" required value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className={`text-sm ${input}`} />
                              </div>
                              <div>
                                <label className={label}>Account</label>
                                <select
                                  aria-label="Account"
                                  value={editAccountId}
                                  onChange={(e) => setEditAccountId(e.target.value === "" ? "" : Number(e.target.value))}
                                  className={`text-sm ${input}`}
                                >
                                  {accounts
                                    .filter((a) => {
                                      if (!editPartner) return true;
                                      if (a.id === editPartner.account_id) return false;
                                      const partnerAcct = accounts.find((x) => x.id === editPartner.account_id);
                                      return partnerAcct ? a.currency === partnerAcct.currency : true;
                                    })
                                    .map((a) => <option key={a.id} value={a.id}>{a.name}{!a.is_active ? " (inactive)" : ""}</option>)}
                                </select>
                              </div>
                              <div>
                                <label className={label}>Category</label>
                                <CategorySelect aria-label="Category" id={`edit-cat-mobile-${tx.id}`} categories={categories} value={editCategoryId} onChange={setEditCategoryId} filterType={editType} className={`text-sm ${input}`} onCategoryCreated={(cat) => setCategories((prev) => [...prev, cat])} />
                              </div>
                              <div>
                                <label className={label}>Status</label>
                                <select aria-label="Status" value={editStatus} onChange={(e) => setEditStatus(e.target.value as "settled" | "pending")} className={`text-sm ${input}`}>
                                  <option value="settled">Settled</option>
                                  <option value="pending">Pending</option>
                                </select>
                              </div>
                              <div>
                                <label className={label}>Type</label>
                                {editPartner ? (
                                  <span
                                    aria-label="Type"
                                    title="Type is fixed for transfer legs."
                                    className={`text-sm flex items-center px-3 rounded border border-border bg-surface text-text-muted h-10`}
                                  >
                                    {editType === "expense" ? "Expense" : "Income"}
                                  </span>
                                ) : (
                                  <select aria-label="Type" value={editType} onChange={(e) => { setEditType(e.target.value as "income" | "expense"); setEditCategoryId(""); }} className={`text-sm ${input}`}>
                                    <option value="expense">Expense</option>
                                    <option value="income">Income</option>
                                  </select>
                                )}
                              </div>
                              <div className="sm:col-span-2">
                                <label className={label}>Amount</label>
                                <input aria-label="Amount" type="number" step="0.01" min="0.01" value={editAmount} onChange={(e) => setEditAmount(e.target.value)} className={`text-sm ${input}`} />
                              </div>
                              {editStatus === "pending" && (
                                <div className="sm:col-span-2" data-testid={`edit-settled-date-cell-mobile-${tx.id}`}>
                                  <label className={label}>Expected settlement date</label>
                                  <input
                                    aria-label="Expected settlement date"
                                    type="date"
                                    min={editDate}
                                    value={editSettledDate}
                                    onChange={(e) => setEditSettledDate(e.target.value)}
                                    className={`text-sm ${input}`}
                                  />
                                </div>
                              )}
                            </div>
                            {/* Promote-to-recurring (L3.12) — mobile layout. Hidden on
                                transfer legs; static chip when already recurring. */}
                            {!editPartner && (
                              <div data-testid={`edit-recurring-row-mobile-${tx.id}`}>
                                {tx.recurring_id !== null ? (
                                  <span
                                    className="inline-flex items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-xs text-text-muted"
                                    data-testid={`edit-recurring-chip-mobile-${tx.id}`}
                                  >
                                    Recurring
                                  </span>
                                ) : (
                                  <div className="flex flex-col gap-2">
                                    <label className="inline-flex items-center gap-2 text-sm text-text-secondary">
                                      <input
                                        type="checkbox"
                                        aria-label="Make recurring"
                                        checked={editPromoteRecurring}
                                        onChange={(e) => setEditPromoteRecurring(e.target.checked)}
                                        className="h-4 w-4"
                                        data-testid={`edit-recurring-toggle-mobile-${tx.id}`}
                                      />
                                      Make recurring
                                    </label>
                                    {editPromoteRecurring && (
                                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                        <div>
                                          <label className={label}>Frequency</label>
                                          <select
                                            aria-label="Frequency"
                                            value={editRecFrequency}
                                            onChange={(e) =>
                                              setEditRecFrequency(
                                                e.target.value as typeof editRecFrequency,
                                              )
                                            }
                                            className={`text-sm ${input}`}
                                          >
                                            <option value="weekly">Weekly</option>
                                            <option value="biweekly">Biweekly</option>
                                            <option value="monthly">Monthly</option>
                                            <option value="quarterly">Quarterly</option>
                                            <option value="yearly">Yearly</option>
                                          </select>
                                        </div>
                                        <div>
                                          <label className={label}>Next due date</label>
                                          <input
                                            aria-label="Next due date"
                                            type="date"
                                            min={todayISO()}
                                            value={editRecNextDue}
                                            onChange={(e) => setEditRecNextDue(e.target.value)}
                                            className={`text-sm ${input}`}
                                          />
                                        </div>
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            )}
                            <div className="flex flex-wrap gap-2 pt-2 border-t border-border-subtle">
                              <button onClick={handleSaveEdit} className="min-h-[44px] px-4 rounded-md bg-accent text-accent-text text-sm font-medium">Save</button>
                              <button onClick={closeEdit} className="min-h-[44px] px-4 rounded-md border border-border text-sm text-text-secondary">Cancel</button>
                            </div>
                          </article>
                        );
                      }
                      return (
                        <article
                          key={tx.id}
                          className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-4 shadow-sm"
                        >
                          {/* Pending rows dim the row contents but keep the
                              status pill at full opacity. CSS opacity composites
                              with ancestor opacity (60%×100% still paints at
                              60%), so we cannot rely on a parent opacity-60 +
                              child opacity-100 override; instead each row segment
                              that should dim sets its own opacity, and the pill
                              cell stays untouched. */}
                          <div
                            className={`flex items-start justify-between gap-2 ${
                              tx.status === "pending" ? "opacity-60" : ""
                            }`}
                          >
                            <input
                              type="checkbox"
                              aria-label={`Select transaction ${tx.id}`}
                              checked={selectedIds.has(tx.id)}
                              onChange={() => toggleOne(tx.id)}
                              className="mt-0.5 h-5 w-5 shrink-0"
                            />
                            <div className="min-w-0 flex-1">
                              <div className="truncate text-sm font-medium text-text-primary">
                                {tx.description}
                              </div>
                              <div className="mt-0.5 text-xs text-text-muted tabular-nums">
                                {tx.date} · {isTransfer && linkedTx ? <>{tx.account_name} &rarr; {linkedTx.account_name}</> : tx.account_name}
                              </div>
                              {tx.status === "pending" && tx.settled_date && tx.settled_date !== tx.date && (
                                <div
                                  className="mt-0.5 text-[10px] text-text-muted"
                                  data-testid={`expected-settled-mobile-${tx.id}`}
                                >
                                  expected settled {tx.settled_date}
                                </div>
                              )}
                            </div>
                            <div className={`shrink-0 text-right text-sm font-semibold tabular-nums ${isTransfer ? "text-accent" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                              {isTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                            </div>
                          </div>
                          <div className="flex items-center gap-2">
                            {tx.category_name && (
                              <div
                                className={`text-xs text-text-secondary truncate ${
                                  tx.status === "pending" ? "opacity-60" : ""
                                }`}
                              >
                                {tx.category_name}
                              </div>
                            )}
                            {isTransfer ? (
                              <span className={`ml-auto rounded px-1.5 py-0.5 text-[10px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-warning-dim text-warning"}`}>
                                {tx.status}
                              </span>
                            ) : (
                              <button
                                onClick={() => handleToggleStatus(tx)}
                                aria-label={`Mark as ${tx.status === "settled" ? "pending" : "settled"}`}
                                className="ml-auto inline-flex min-h-[44px] items-center justify-center"
                              >
                                {/* Outer button = WCAG 2.5.8 hit area;
                                    inner span = lean pill visual. */}
                                <span
                                  className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                                    tx.status === "settled"
                                      ? "bg-success-dim text-success"
                                      : "bg-warning-dim text-warning"
                                  }`}
                                >
                                  {tx.status}
                                </span>
                              </button>
                            )}
                          </div>
                          <div
                            className={`flex flex-wrap gap-2 pt-2 border-t border-border-subtle ${
                              tx.status === "pending" ? "opacity-60" : ""
                            }`}
                          >
                            <button
                              onClick={() => startEdit(tx)}
                              aria-label={`Edit: ${tx.description}`}
                              disabled={bulkDeleting}
                              className="min-h-[44px] px-3 rounded-md border border-border text-sm text-text-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                            >
                              Edit
                            </button>
                            {!isTransfer && (
                              <button
                                onClick={() => setMarkModalSource(tx)}
                                aria-label={`Mark as transfer: ${tx.description}`}
                                disabled={bulkDeleting}
                                className="min-h-[44px] px-3 rounded-md border border-border text-sm text-text-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                              >
                                Mark as transfer…
                              </button>
                            )}
                            {isTransfer && (
                              <button
                                onClick={() => openUnpairModal(tx)}
                                aria-label={`Unlink transfer: ${tx.description}`}
                                disabled={bulkDeleting}
                                className="min-h-[44px] px-3 rounded-md border border-border text-sm text-text-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                              >
                                Unlink
                              </button>
                            )}
                            <button
                              onClick={() => setConfirmDeleteId(tx.id)}
                              aria-label={`Delete: ${tx.description}`}
                              disabled={bulkDeleting}
                              className="min-h-[44px] px-3 rounded-md border border-border text-sm text-danger disabled:opacity-40 disabled:cursor-not-allowed"
                            >
                              Delete
                            </button>
                          </div>
                        </article>
                      );
                    })}
                    {visibleTxs.length === 0 && (
                      <div className="px-4 py-8 text-center text-sm text-text-muted">
                        {activeAccounts.length === 0
                          ? "Create an account first."
                          : categories.length === 0
                            ? "Create a category first."
                            : "No transactions match your filters."}
                      </div>
                    )}
                  </div>
                </>
              );
            })()}
          </div>

          {(page > 0 || hasMore) && (
            <div className="mt-4 flex items-center justify-between">
              <button onClick={() => setPage(Math.max(0, page - 1))} disabled={page === 0 || bulkDeleting} className="rounded-md border border-border px-3 py-1.5 text-sm text-text-secondary hover:bg-surface-raised disabled:opacity-40">
                Previous
              </button>
              <span className="text-xs text-text-muted">Page {page + 1}</span>
              <button onClick={() => setPage(page + 1)} disabled={!hasMore || bulkDeleting} className="rounded-md border border-border px-3 py-1.5 text-sm text-text-secondary hover:bg-surface-raised disabled:opacity-40">
                Next
              </button>
            </div>
          )}
        </>
      )}
      <ConfirmModal
        open={confirmDeleteId !== null}
        title="Delete Transaction"
        message="Delete this transaction?"
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => confirmDeleteId !== null && handleDelete(confirmDeleteId)}
        onCancel={() => setConfirmDeleteId(null)}
      />
      <ConfirmModal
        open={confirmBulkDelete}
        title="Delete transactions"
        message={`Delete ${selectedIds.size} selected transaction${selectedIds.size === 1 ? "" : "s"}? This cannot be undone. Balances will be adjusted for settled transactions.`}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={handleBulkDelete}
        onCancel={() => setConfirmBulkDelete(false)}
      />
      {linkModalLegs && (
        <LinkAsTransferModal
          expenseLeg={linkModalLegs.expense}
          incomeLeg={linkModalLegs.income}
          onLinked={() => {
            setLinkModalLegs(null);
            clearSelection();
            loadTransactions(page).catch(() => {});
          }}
          onCancel={() => setLinkModalLegs(null)}
        />
      )}
      {markModalSource && (
        <MarkAsTransferModal
          source={markModalSource}
          accounts={accounts}
          onConverted={() => {
            setMarkModalSource(null);
            loadTransactions(page).catch(() => {});
          }}
          onCancel={() => setMarkModalSource(null)}
        />
      )}
      {unpairModalLegs && (
        <UnpairTransferModal
          expenseLeg={unpairModalLegs.expense}
          incomeLeg={unpairModalLegs.income}
          categories={categories}
          onUnpaired={() => {
            setUnpairModalLegs(null);
            loadTransactions(page).catch(() => {});
          }}
          onCancel={() => setUnpairModalLegs(null)}
        />
      )}
    </AppShell>
  );
}
