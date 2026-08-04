"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import Tooltip from "@/components/Tooltip";
import HelpTooltip from "@/components/help/HelpTooltip";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { equalsAmount, formatAmount, formatLocalDate, toEditAmount, todayISO } from "@/lib/format";
import { isOpenPeriod } from "@/lib/billingPeriodStatus";
import { input, label, badgeNeutral, btnPrimary, btnSecondary, btnDangerSolid, card, error as errorCls, pageTitle, stickyBar } from "@/lib/styles";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";
import { useAccounts } from "@/lib/hooks/use-accounts";
import { useCategories } from "@/lib/hooks/use-categories";
import { useBillingPeriods } from "@/lib/hooks/use-billing-periods";
import CategorySelect from "@/components/ui/CategorySelect";
import type { Account, BillingPeriod, Category, Transaction } from "@/lib/types";

// Stable empty-array fallbacks so a still-loading SWR ref (data === undefined)
// yields the same reference across renders, keeping memo/callback deps stable.
const EMPTY_ACCOUNTS: Account[] = [];
const EMPTY_CATEGORIES: Category[] = [];
const EMPTY_PERIODS: BillingPeriod[] = [];

import ConfirmModal from "@/components/ui/ConfirmModal";
import LinkAsTransferModal from "@/components/transactions/LinkAsTransferModal";
import MarkAsTransferModal from "@/components/transactions/MarkAsTransferModal";
import UnpairTransferModal from "@/components/transactions/UnpairTransferModal";
import BatchEditModal from "@/components/transactions/BatchEditModal";
import TagChipInput from "@/components/transactions/TagChipInput";
import SuggestCategoryButton from "@/components/transactions/SuggestCategoryButton";
import { SetUpAiCta } from "@/components/ai/SetUpAiCta";
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import ResetSortFiltersButton from "@/components/ui/ResetSortFiltersButton";
import {
  FILTERS_KEY_TRANSACTIONS,
  PAGE_SIZE_KEY_TRANSACTIONS,
  SORT_KEY_TRANSACTIONS,
} from "@/lib/hooks/persisted-keys";
import { usePersistedFilters } from "@/lib/hooks/use-persisted-filters";
import { usePersistedSort } from "@/lib/hooks/use-persisted-sort";
import Pagination from "@/components/ui/Pagination";
import { pageCount } from "@/lib/hooks/use-table-state";

// TBD-295 copy discipline. A one-way `linked_transaction_id` has producers
// other than reconciliation (a self-link, a cross-org link, an A->B->C chain),
// so the copy must never assert reconciliation as the cause. It says what the
// row IS and what follows from it. Declared once so the desktop and mobile
// twins cannot drift apart, which is how the mobile slot gets missed.
const MATCHED_BADGE_TITLE =
  "Marked as a duplicate of another transaction. It is excluded from balances and reports.";
const MATCHED_BADGE_SR =
  "marked as a duplicate of another transaction, excluded from balances and reports";
// Shown when a `?transaction_id=` deep link points at a row the current page
// (filters + pagination) does not contain. The effect used to return silently,
// so following the badge from a filtered list looked like a dead link.
const DEEP_LINK_MISS =
  "That transaction isn't on this page. Clear your filters to find it.";

// TBD-294. Deleting a row that another row was marked a duplicate OF marks
// that other row rejected. REJECTED is terminal and unreachable through the
// edit API, so the change is irreversible — the user has to be told.
function demotionNotice(demotedIds: number[]): string {
  if (demotedIds.length === 0) return "";
  const n = demotedIds.length;
  return n === 1
    ? "1 matched duplicate was marked rejected. It no longer counts toward balances or reports."
    : `${n} matched duplicates were marked rejected. They no longer count toward balances or reports.`;
}



const DATE_PARAM_RE = /^\d{4}-\d{2}-\d{2}$/;

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
  const role = user?.role ?? null;
  const ai = useAiStatus();
  const categorizeAi = ai?.categorize;
  const searchParams = useSearchParams();
  const urlFiltersSyncedRef = useRef(false);
  const categoryUrlSyncedRef = useRef(false);
  const targetDesktopRowRef = useRef<HTMLDivElement | null>(null);
  const targetMobileRowRef = useRef<HTMLElement | null>(null);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  // Reference data via shared SWR hooks (SWR Phase 2). Gated on resolved
  // auth so a fetch never fires before the bearer token is set. ``mutate``
  // is how the post-write event and inline category-create force a refresh.
  const refsEnabled = !loading && !!user;
  const { data: accountsData, mutate: mutateAccounts } = useAccounts(refsEnabled);
  const { data: categoriesData, mutate: mutateCategories } = useCategories(refsEnabled);
  const { data: periodsData, error: periodsError, mutate: mutateBillingPeriods } = useBillingPeriods(refsEnabled);
  const accounts = accountsData ?? EMPTY_ACCOUNTS;
  const categories = categoriesData ?? EMPTY_CATEGORIES;
  const periods = periodsData ?? EMPTY_PERIODS;
  const periodsLoaded = periodsData !== undefined;
  // "Settled" = resolved OR errored. The initial list fetch waits for this so a
  // period filter resolves against real periods and the list is fetched once
  // (not once on the empty fallback, then again when periods land — the #519
  // double-fetch). Treating an error as settled keeps a failed periods request
  // from blanking the whole list: it just loads without period-range filtering.
  const periodsSettled = periodsData !== undefined || periodsError !== undefined;
  // Defense in depth: a billing-periods request that never settles (a stalled
  // connection that neither resolves nor errors) must not strand the list on
  // the spinner forever now that the initial fetch waits on periods. After a
  // generous delay we let the list load anyway; if periods do eventually arrive
  // it re-fetches with the real range, matching the old periods-independent
  // behavior for this rare case.
  const [periodsWaitElapsed, setPeriodsWaitElapsed] = useState(false);
  const canLoadList = periodsSettled || periodsWaitElapsed;
  const [error, setError] = useState("");
  // TBD-294: a non-error, non-blocking outcome banner. The demotion is a
  // side effect of a successful delete, so it must not render as an error.
  const [notice, setNotice] = useState("");
  // Non-blocking refresh-error state for the AppShell post-write event
  // listener. The page keeps the previous list; banner offers a Retry.
  const [refreshError, setRefreshError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [fetching, setFetching] = useState(true);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [pageSize, setPageSize] = useState<number>(() => {
    if (typeof window === "undefined") return 25;
    const raw = window.localStorage.getItem(PAGE_SIZE_KEY_TRANSACTIONS);
    const n = raw ? Number(raw) : 25;
    return [10, 25, 50, 100].includes(n) ? n : 25;
  });

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
  // PR-Tags-A: chip-managed tag set for the edit form. Persisted via
  // PUT /api/v1/transactions/{id}/tags after the PUT to /transactions/{id}.
  const [editTags, setEditTags] = useState<string[]>([]);
  // Edit-time promote-to-recurring (L3.12). Hidden on rows that are already
  // recurring (a static chip is rendered instead). Default next_due_date is
  // "today + 30 days" so users get a reasonable starting point without a
  // backend round-trip.
  const [editPromoteRecurring, setEditPromoteRecurring] = useState(false);
  const [editRecFrequency, setEditRecFrequency] = useState<
    "weekly" | "biweekly" | "monthly" | "quarterly" | "yearly"
  >("monthly");
  const [editRecNextDue, setEditRecNextDue] = useState("");
  // TBD-275: total instalments the promoted series delivers, INCLUDING the row
  // being edited. Blank = open-ended. Kept as a STRING so "not filled in" and
  // "filled in with 0" stay distinguishable — a `number | ""` state would make
  // the blank case indistinguishable from a cleared field mid-edit.
  const [editRecOccurrenceCount, setEditRecOccurrenceCount] = useState("");

  // Filters: persisted via localStorage so a navigate-away-and-back, or a
  // tab reload, lands the user back on the same view. Item 6 of the
  // launch-prep punch list.
  type TxFilters = {
    filterAccount: number | "";
    filterCategory: number | "";
    filterType: string;
    filterStatus: string;
    filterDateFrom: string;
    filterDateTo: string;
    filterSearch: string;
    filterPeriod: string;
  };
  const TX_FILTER_DEFAULTS: TxFilters = {
    filterAccount: "",
    filterCategory: "",
    filterType: "",
    filterStatus: "",
    filterDateFrom: "",
    filterDateTo: "",
    filterSearch: "",
    filterPeriod: "",
  };
  const persistedFilters = usePersistedFilters<TxFilters>(
    FILTERS_KEY_TRANSACTIONS,
    TX_FILTER_DEFAULTS,
  );
  const {
    filterAccount,
    filterCategory,
    filterType,
    filterStatus,
    filterDateFrom,
    filterDateTo,
    filterSearch,
    filterPeriod,
  } = persistedFilters.filters;
  // setField is memoized inside the hook; hoist it to a stable identifier so
  // the useCallback-wrapped setters below get a clean, stable dependency.
  const persistedSetField = persistedFilters.setField;
  const setFilterAccount = (v: number | "") =>
    persistedSetField("filterAccount", v);
  // Stable across renders (setField is memoized) so effects that call this
  // setter can list it in their dep array without re-running every render.
  const setFilterCategory = useCallback(
    (v: number | "") => persistedSetField("filterCategory", v),
    [persistedSetField],
  );
  const setFilterType = (v: string) =>
    persistedSetField("filterType", v);
  const setFilterStatus = (v: string) =>
    persistedSetField("filterStatus", v);
  const setFilterDateFrom = (v: string) =>
    persistedSetField("filterDateFrom", v);
  const setFilterDateTo = (v: string) =>
    persistedSetField("filterDateTo", v);
  const setFilterSearch = (v: string) =>
    persistedSetField("filterSearch", v);
  // Stable across renders (setField is memoized) so effects that call this
  // setter can list it in their dep array without re-running every render.
  const setFilterPeriod = useCallback(
    (v: string) => persistedSetField("filterPeriod", v),
    [persistedSetField],
  );

  const persistedSort = usePersistedSort<SortField>(
    SORT_KEY_TRANSACTIONS,
    "date",
    "desc",
    [
      "date",
      "description",
      "account_name",
      "category_name",
      "status",
      "amount",
    ] as const,
  );
  const sortField = persistedSort.field;
  const sortDir = persistedSort.dir;

  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [showBatchEdit, setShowBatchEdit] = useState(false);
  const [batchEditing, setBatchEditing] = useState(false);

  // Transfer modals
  const [linkModalLegs, setLinkModalLegs] = useState<{ expense: Transaction; income: Transaction } | null>(null);
  const [markModalSource, setMarkModalSource] = useState<Transaction | null>(null);
  const [unpairModalLegs, setUnpairModalLegs] = useState<{ expense: Transaction; income: Transaction } | null>(null);
  // Partner row for the currently-edited linked transaction. Hydrated from
  // the visible list when possible, otherwise fetched on demand. Used to
  // filter the Account select and to render the mirror-amount notice.
  const [editPartner, setEditPartner] = useState<Transaction | null>(null);

  const loadTransactions = useCallback(async (p: number) => {
    // The demotion notice is scoped to the delete that produced it. Without
    // this it survived filter changes, page changes and edits — it was only
    // ever cleared by the NEXT delete, so a warning about rows the user can
    // no longer see stayed on screen indefinitely.
    //
    // Safe against its own writers: handleDelete / handleBulkDelete await
    // this call and set the notice AFTERWARDS, so the clear can never race
    // ahead of the message it is meant to precede.
    setNotice("");
    // collapse_transfers=true (TBD-268): the server folds each MUTUALLY-linked
    // transfer pair to one row BEFORE applying the limit, so a page of
    // `pageSize` rows is `pageSize` transfers. This replaces a client-side
    // hide that ran AFTER the server's LIMIT and therefore short-changed every
    // page — and blanked the list entirely under `type=income`.
    let url = `/api/v1/transactions?limit=${pageSize}&offset=${p * pageSize}&collapse_transfers=true`;
    url += `&sort_by=${encodeURIComponent(sortField)}&sort_dir=${encodeURIComponent(sortDir)}`;
    if (filterAccount) url += `&account_id=${filterAccount}`;
    if (filterCategory) url += `&category_id=${filterCategory}`;
    if (filterType) url += `&type=${filterType}`;
    if (filterStatus) url += `&status=${filterStatus}`;

    // Period filter overrides date_from/date_to
    if (filterPeriod) {
      const per = periods.find((pp) => String(pp.id) === filterPeriod);
      // TBD-242: an OPEN period has no end_date to bound the query with, so
      // the filter is skipped. Only closed periods are offered (below), so
      // this is defensive rather than reachable.
      if (per && !isOpenPeriod(per)) {
        url += `&date_from=${per.start_date}`;
        url += `&date_to=${per.end_date}`;
      }
    } else {
      if (filterDateFrom) url += `&date_from=${filterDateFrom}`;
      if (filterDateTo) url += `&date_to=${filterDateTo}`;
    }

    if (filterSearch) url += `&search=${encodeURIComponent(filterSearch)}`;
    const data = await apiFetch<{ items: Transaction[]; total: number }>(url);
    setTransactions(data?.items ?? []);
    setTotal(data?.total ?? 0);
    setFetching(false);
  }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo, filterSearch, filterPeriod, periods, pageSize, sortField, sortDir]);

  // Reference data (accounts/categories/periods) auto-fetches via the SWR
  // hooks above once ``refsEnabled`` flips true — no explicit mount effect.

  // Apply supported URL params once so dashboard deep links don't fight
  // user-edited filters after initial hydration.
  useEffect(() => {
    if (urlFiltersSyncedRef.current) return;
    urlFiltersSyncedRef.current = true;

    const patch: Partial<TxFilters> = {};
    const accountId = Number(searchParams.get("account_id"));
    if (Number.isInteger(accountId) && accountId > 0) {
      patch.filterAccount = accountId;
    }

    const dateFrom = searchParams.get("date_from");
    const dateTo = searchParams.get("date_to");
    if (dateFrom && DATE_PARAM_RE.test(dateFrom)) {
      patch.filterDateFrom = dateFrom;
      patch.filterPeriod = "";
    }
    if (dateTo && DATE_PARAM_RE.test(dateTo)) {
      patch.filterDateTo = dateTo;
      patch.filterPeriod = "";
    }

    if (Object.keys(patch).length > 0) {
      persistedFilters.set(patch);
    }
  }, [persistedFilters, searchParams]);

  // Apply ?category= URL param once categories are loaded
  useEffect(() => {
    if (categoryUrlSyncedRef.current) return;
    const categoryName = searchParams.get("category");
    if (categoryName && categories.length > 0) {
      const match = categories.find(
        (c) => c.name.toLowerCase() === categoryName.toLowerCase()
      );
      if (match) {
        categoryUrlSyncedRef.current = true;
        setFilterCategory(match.id);
      }
    }
  }, [categories, searchParams, setFilterCategory]);

  // TBD-242: the dropdown offers only CLOSED periods — an open period has no
  // end bound, so it cannot express a date range.
  const closedPeriods = useMemo(
    () => periods.filter((p) => !isOpenPeriod(p)),
    [periods],
  );

  useEffect(() => {
    if (!periodsLoaded || !filterPeriod) return;
    const selectedClosedPeriod = closedPeriods.some(
      (p) => String(p.id) === filterPeriod,
    );
    if (!selectedClosedPeriod) setFilterPeriod("");
  }, [closedPeriods, filterPeriod, periodsLoaded, setFilterPeriod]);

  // Arm the stalled-periods fallback only while we are actually waiting.
  useEffect(() => {
    if (loading || !user || periodsSettled) return;
    const timer = setTimeout(() => setPeriodsWaitElapsed(true), 10000);
    return () => clearTimeout(timer);
  }, [loading, user, periodsSettled]);

  useEffect(() => {
    if (!loading && user && canLoadList) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch loading flag raised before the async transaction list load kicks off
      setFetching(true);
      loadTransactions(page).catch(() => setFetching(false));
    }
  }, [loading, user, canLoadList, loadTransactions, page]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset pagination to the first page whenever any filter selection changes
    setPage(0);
  }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo, filterSearch, filterPeriod]);

  // Clamp the page after a refetch shrinks the result set (e.g. a bulk
  // delete that empties the last page) so the user is never stranded on a
  // page beyond the new total.
  useEffect(() => {
    const last = pageCount(total, pageSize) - 1;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- clamp the current page down after a refetch shrinks the result set past it
    if (page > last) setPage(Math.max(0, last));
  }, [total, pageSize, page]);

  // After a write from the AppShell-level "+ New Transaction" CTA the
  // page must re-pull the visible list (the new row should appear) and
  // the refs (a freshly-created category from inside the panel must
  // show in the filter dropdown). Promise.allSettled keeps both fetches
  // independent so a transient ref failure doesn't block the list
  // refresh, and any rejection surfaces the inline retry banner below.
  const refreshAfterTransactionAdded = useCallback(async () => {
    if (loading || !user) return;
    setRefreshing(true);
    // Revalidate each SWR ref (a freshly-created category must show in the
    // filter dropdown) and re-pull the visible list. allSettled keeps them
    // independent so a transient ref failure doesn't block the list refresh,
    // and any rejection surfaces the inline retry banner.
    const results = await Promise.allSettled([
      mutateAccounts(),
      mutateCategories(),
      mutateBillingPeriods(),
      loadTransactions(page),
    ]);
    setRefreshing(false);
    setRefreshError(results.some((r) => r.status === "rejected"));
  }, [loading, user, mutateAccounts, mutateCategories, mutateBillingPeriods, loadTransactions, page]);

  useTransactionAddedListener(() => {
    void refreshAfterTransactionAdded();
  });

  // Clear selection whenever the visible row set changes (filters, sort, page,
  // or page size) so navigation never leaves an invisible selection behind.
  useEffect(() => {
    clearSelection();
  }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo, filterSearch, filterPeriod, sortField, sortDir, page, pageSize]);

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

  // Selection operates on every returned row. The server collapses each
  // MUTUALLY-linked transfer pair to a single leg (collapse_transfers=true on
  // the list request), so one row here is one transfer and a page of N rows is
  // N transfers. Deleting the surviving leg still cascades to its partner
  // server-side (delete_transaction / bulk_delete_transactions, which also
  // dedupes ids), so a selection built from these rows deletes whole
  // transfers, never halves — that cascade is why we can safely offer one row
  // per pair, NOT why a row would be missing.
  //
  // Do NOT reintroduce a client-side hide. It cannot see the partner when a
  // filter (account_id, type) or a page boundary excludes it, which is exactly
  // how TBD-268 rendered zero rows against a non-zero total.
  const allPageSelected =
    transactions.length > 0 && transactions.every((t) => selectedIds.has(t.id));
  const somePageSelected =
    transactions.some((t) => selectedIds.has(t.id)) && !allPageSelected;

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
        transactions.forEach((t) => next.delete(t.id));
      } else {
        transactions.forEach((t) => next.add(t.id));
      }
      return next;
    });
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  function changePageSize(n: number) {
    setPageSize(n);
    setPage(0);
    if (typeof window !== "undefined") window.localStorage.setItem(PAGE_SIZE_KEY_TRANSACTIONS, String(n));
  }

  async function handleDelete(id: number) {
    setConfirmDeleteId(null);
    setError("");
    setNotice("");
    try {
      // TBD-294: the endpoint returns a body now. Deleting a row that another
      // row was matched against marks that other row rejected, irreversibly
      // through every API we expose — so it is never silent.
      const res = await apiFetch<{ deleted: boolean; demoted_ids: number[] }>(
        `/api/v1/transactions/${id}`,
        { method: "DELETE" },
      );
      await loadTransactions(page);
      setNotice(demotionNotice(res?.demoted_ids ?? []));
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleBulkDelete() {
    setConfirmBulkDelete(false);
    setError("");
    setNotice("");
    setBulkDeleting(true);
    try {
      const body = { ids: Array.from(selectedIds) };
      const res = await apiFetch<{
        requested_count: number;
        deleted_count: number;
        skipped_ids: number[];
        demoted_ids: number[];
      }>("/api/v1/transactions/bulk-delete", {
        method: "POST",
        body: JSON.stringify(body),
      });
      clearSelection();
      await loadTransactions(page);
      setNotice(demotionNotice(res?.demoted_ids ?? []));
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

  async function handleBatchEdit(payload: {
    category_id?: number; status?: "settled" | "pending"; account_id?: number; tags?: string[];
  }) {
    setShowBatchEdit(false);
    setError("");
    setBatchEditing(true);
    try {
      const res = await apiFetch<{
        requested_count: number;
        updated_count: number;
        skipped: { id: number; reason: string }[];
      }>("/api/v1/transactions/bulk-update", {
        method: "POST",
        body: JSON.stringify({ ids: Array.from(selectedIds), ...payload }),
      });
      clearSelection();
      await loadTransactions(page);
      if (res.skipped.length > 0) {
        setError(
          `Updated ${res.updated_count} of ${res.requested_count}. ${res.skipped.length} skipped: ${res.skipped
            .slice(0, 3)
            .map((s) => s.reason)
            .join("; ")}${res.skipped.length > 3 ? "…" : ""}`,
        );
      }
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setBatchEditing(false);
    }
  }

  async function openUnpairModal(tx: Transaction) {
    // TBD-268: gate on the MUTUALITY-verified signal, not on the raw column.
    // A one-way reconciliation match also carries linked_transaction_id, and
    // unpair_transactions does not check reciprocity — unpairing one would
    // silently rewrite the unrelated canonical row's category. (The
    // server-side reciprocity check in unpair_transactions is a separate
    // ticket; this closes the path that reaches it from the list.)
    if (!tx.linked_transaction_id || tx.linked_account_name == null) return;
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
    // Seed the chip input with the row's current tags. ``tags`` is
    // always present on TransactionResponse (backend selectinload),
    // but defensively coerce to [] in case of a partial test fixture.
    setEditTags((tx.tags ?? []).map((t) => t.name));
    setEditPromoteRecurring(false);
    setEditRecFrequency("monthly");
    setEditRecNextDue(defaultNextDueISO());
    setEditRecOccurrenceCount("");
    // Hydrate partner for linked rows so the Account select can filter
    // currency-compatible options and the mirror-amount notice can render.
    //
    // TBD-268: gated on the same mutuality-verified signal every rendered
    // affordance uses. A one-way reconciliation match also carries
    // `linked_transaction_id`, and hydrating its partner would print
    // "Editing a transfer leg. Changes to amount apply to both rows." above a
    // form that labels its picker "Category" — the row contradicting itself
    // again — and would freeze Type on what is an ordinary transaction.
    if (tx.linked_transaction_id && tx.linked_account_name != null) {
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
    setEditTags([]);
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
    // TBD-268: "transfer leg" is `linked_account_name != null`, matching the
    // `!editPartner` gate that decides whether the checkbox renders at all —
    // otherwise a reconcile-matched row shows a checkbox that silently does
    // nothing when ticked.
    const editingRow = transactions.find((t) => t.id === editingId) ?? null;
    const wantsPromote =
      editPromoteRecurring &&
      editingRow !== null &&
      // TBD-295: the RAW column, not `linked_account_name`. The server guard
      // in `promote_to_recurring` is `linked_transaction_id is not None` --
      // it asks "may this row seed a repeating series", not "is this a
      // transfer leg" -- so a matched row is refused there too. Gating on
      // `linked_account_name` here left the identical hole the render sites
      // had: a checkbox that ticks and then 400s. Invisible until TBD-292
      // stopped the edit itself from 409-ing first.
      editingRow.linked_transaction_id === null &&
      editingRow.recurring_id === null;
    if (wantsPromote && !editRecNextDue) {
      setError("Pick a next due date");
      return;
    }
    // ⚠ TBD-301: there is deliberately NO "today or later" check here, and no
    // `min` on either next-due-date input. The server's lower bound is the
    // start of the org's CURRENT billing cycle (TBD-283), not `today`, so a
    // client rule keyed on `today` refuses dates the API accepts: every org
    // whose cycle does not begin today has a legal window this page used to
    // reject. The bound depends on `billing_cycle_day` and is not derivable
    // here, so the client defers. The server's 400 names both the boundary
    // and the remedy, and reaches the user through the partial-success
    // banner in the promote catch below.
    // TBD-275. Blank is valid (open-ended); anything else must be a whole
    // number >= 1. Checked BEFORE the PUT, alongside the two guards above, so a
    // bad count never leaves the user with a committed edit and a
    // partial-success banner.
    const trimmedRecCount = editRecOccurrenceCount.trim();
    if (wantsPromote && trimmedRecCount !== "") {
      const parsedRecCount = Number(trimmedRecCount);
      if (!Number.isInteger(parsedRecCount) || parsedRecCount < 1) {
        setError("Number of payments must be a whole number of 1 or more");
        return;
      }
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
      // PR-Tags-A: replace the tag set. Sent on every edit (including
      // when the user cleared every chip) so the PUT is authoritative.
      await apiFetch(`/api/v1/transactions/${editingId}/tags`, {
        method: "PUT",
        body: JSON.stringify({ tag_names: editTags }),
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
                // TBD-275. Blank OMITS the key entirely — `null` and `0` are
                // both wrong on the wire (the schema is
                // `Optional[int] = Field(gt=0)`, so 0 is a 422 and null is a
                // noisier spelling of "absent").
                ...(trimmedRecCount !== ""
                  ? { occurrence_count: Number(trimmedRecCount) }
                  : {}),
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
      persistedSort.setSort(field, sortDir === "asc" ? "desc" : "asc");
    } else {
      persistedSort.setSort(field, SORT_DEFAULTS[field]);
    }
    setPage(0);
  }
  const targetTransactionId = useMemo(() => {
    const raw = searchParams.get("transaction_id");
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  }, [searchParams]);

  // TBD-295: the effect below used to `return` silently when the target was
  // not in the loaded page, which reads as a dead link — and the matched badge
  // now sends users through this mechanism from a filtered list, where a miss
  // is the COMMON case, not the edge one.
  //
  // DERIVED, not effect state: this is a pure function of the URL param, the
  // loaded page and the in-flight flag. A useState + useEffect pair would
  // render one frame stale AND trip `react-hooks/set-state-in-effect`. The
  // `!fetching` term keeps the message off the first paint, when the list is
  // legitimately empty only because the request has not landed.
  const deepLinkMiss =
    targetTransactionId !== null &&
    !fetching &&
    !transactions.some((tx) => tx.id === targetTransactionId);

  useEffect(() => {
    if (
      targetTransactionId === null ||
      !transactions.some((tx) => tx.id === targetTransactionId)
    ) {
      return;
    }
    const prefersDesktop =
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function" ||
      window.matchMedia("(min-width: 768px)").matches;
    const row = prefersDesktop
      ? targetDesktopRowRef.current ?? targetMobileRowRef.current
      : targetMobileRowRef.current ?? targetDesktopRowRef.current;
    row?.scrollIntoView({ block: "center", behavior: "auto" });
  }, [targetTransactionId, transactions]);

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
        <div className={`${stickyBar} mb-4 flex items-center justify-between gap-3 py-3`}>
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
              <span className="inline-flex items-center gap-1">
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
                <Tooltip
                  content="Pair one expense and one income of the same amount across two accounts so the app treats them as a single transfer instead of two separate transactions."
                  learnMoreSection="transactions"
                  triggerLabel="More about transfer pairing"
                />
              </span>
            )}
            <button
              type="button"
              className={`${btnSecondary} inline-flex min-h-[44px] items-center`}
              onClick={() => setShowBatchEdit(true)}
              disabled={bulkDeleting || batchEditing}
            >
              {batchEditing ? "Applying…" : "Batch edit"}
            </button>
            <button
              type="button"
              className={`${btnDangerSolid} inline-flex min-h-[44px] items-center`}
              onClick={() => setConfirmBulkDelete(true)}
              disabled={bulkDeleting || batchEditing}
            >
              {bulkDeleting ? "Deleting…" : "Delete selected"}
            </button>
          </div>
        </div>
      )}
      <div className="mb-8 flex items-center justify-between">
        <div className="flex items-start gap-1" data-tour-id="transactions.title">
          <h1 className={`${pageTitle} mb-0`}>Transactions</h1>
          <HelpAnchor section="transactions" label="Transactions" />
        </div>
        <div className="flex items-center gap-2">
          <Link href="/transactions/batch" className={btnSecondary}>
            Batch entry
          </Link>
          <Link href="/import" className={btnSecondary}>
            Import
          </Link>
        </div>
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {/* The LIVE REGION is mounted unconditionally and only its CONTENT
          changes. `role="status"` on a div that is itself conditionally
          mounted is unreliable: many screen readers only announce mutations
          inside a region that already existed when the mutation happened, so
          a region that appears together with its first message is frequently
          announced never. The visible box stays conditional (and keeps the
          testid) — it is the announcer that has to be permanent. */}
      <div role="status" aria-live="polite" data-testid="transactions-live-region">
        {notice && (
          <div
            className="mb-6 rounded-md border border-border bg-surface-raised px-4 py-3 text-sm text-text-secondary"
            data-testid="transactions-notice"
          >
            {notice}
          </div>
        )}

        {deepLinkMiss && (
          <div
            className="mb-6 rounded-md border border-border bg-surface-raised px-4 py-3 text-sm text-text-secondary"
            data-testid="deep-link-miss"
          >
            {DEEP_LINK_MISS}
          </div>
        )}
      </div>

      {refreshError && (
        <div
          className={`mb-6 flex items-center justify-between gap-3 ${errorCls}`}
          role="status"
          data-testid="transactions-refresh-error"
        >
          <span>Failed to refresh after the last update. Try again.</span>
          <button
            type="button"
            onClick={() => {
              setRefreshError(false);
              void refreshAfterTransactionAdded();
            }}
            disabled={refreshing}
            className="rounded-md border border-danger/40 px-3 py-1 text-xs font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
          >
            {refreshing ? "Retrying..." : "Retry"}
          </button>
        </div>
      )}

      {/* Search + Preset filters */}
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center sm:gap-3">
        <div className="w-full sm:flex-1 sm:min-w-[200px]">
          <label htmlFor="f-search" className="sr-only">Search transactions</label>
          <input id="f-search" type="text" placeholder="Search by description or amount..." value={filterSearch} onChange={(e) => setFilterSearch(e.target.value)} className={input} />
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
                    formatLocalDate(new Date(now.getFullYear(), now.getMonth() + 1, 0)),
                  );
                },
              },
              {
                label: "All",
                fn: () => setRange("", ""),
              },
            ];
            return presets.map((p) => (
              <button key={p.label} type="button" onClick={p.fn} className="rounded-md border border-border px-2.5 py-1 text-[11px] text-text-secondary hover:bg-surface-raised min-h-[44px] sm:min-h-0">
                {p.label}
              </button>
            ));
          })()}
          {/* Item 6: Reset affordance. Visible only when sort or any filter
              differs from defaults so the toolbar stays clean for new
              users. Clears localStorage for both keys. */}
          <ResetSortFiltersButton
            visible={!persistedFilters.isDefault || !persistedSort.isDefault}
            onClick={() => {
              persistedFilters.reset();
              persistedSort.reset();
            }}
          />
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
        {closedPeriods.length > 0 && (
          <div className="w-full sm:w-auto">
            <label htmlFor="f-period" className="sr-only">Billing period</label>
            <select id="f-period" value={filterPeriod} onChange={(e) => { setFilterPeriod(e.target.value); if (e.target.value) { setFilterDateFrom(""); setFilterDateTo(""); } }} className={`w-full sm:w-40 ${input}`}>
              <option value="">All periods</option>
              {closedPeriods.map((p) => (
                <option key={p.id} value={String(p.id)}>
                  {p.start_date} – {p.end_date}
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
                  { field: "date" as const, label: "Date", span: "col-span-1", align: "", sortable: true },
                  { field: "settled_date" as const, label: "Settled", span: "col-span-1", align: "", sortable: false },
                  { field: "description" as const, label: "Description", span: "col-span-2", align: "", sortable: true },
                  { field: "account_name" as const, label: "Account", span: "col-span-2", align: "", sortable: true },
                  { field: "category_name" as const, label: "Category", span: "col-span-1", align: "", sortable: true },
                  { field: "status" as const, label: "Status", span: "col-span-1", align: "text-center", sortable: true },
                  { field: "amount" as const, label: "Amount", span: "col-span-1", align: "text-right", sortable: true },
                ]).map((col) =>
                  // Settled is a display-only column (the server sorts by `date`),
                  // so it renders as a static header rather than a sort button.
                  col.sortable ? (
                    <button key={col.field} onClick={() => toggleSort(col.field as SortField)} className={`${col.span} ${col.align} min-h-[32px] hover:text-text-primary transition-colors`}>
                      {col.label}{sortField === col.field ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                    </button>
                  ) : (
                    <span key={col.field} className={`${col.span} ${col.align} flex items-center`}>{col.label}</span>
                  ),
                )}
                <span className="col-span-2" />
              </div>
            </div>
            {(() => {
              return (
                <>
                  {/* Desktop/tablet grid rows (md+) */}
                  <div className="hidden md:block divide-y divide-border-subtle">
                    {transactions.map((tx) => {
                      // TBD-268: `linked_account_name` is the ONE transfer
                      // signal this row renders from, and it is
                      // mutuality-verified — the server populates it only for
                      // a reciprocal, same-org pair. `linked_transaction_id`
                      // alone also matches a one-way reconciliation match,
                      // which is NOT a transfer.
                      //
                      // Do NOT re-split this into two signals. Driving some
                      // affordances off the raw column and others off this one
                      // makes a reconcile-matched row claim to be a transfer in
                      // the amount cell, the status cell and the category
                      // picker while claiming not to be one in the subline and
                      // the Unlink slot.
                      const isPairedTransfer = tx.linked_account_name != null;
                      // TBD-289: `isPairedTransfer` above stays the ONE signal
                      // that decides whether a row RENDERS AS a transfer (arrow
                      // subline, brass amount, transfer category picker, Unlink).
                      // This second flag decides something different and must
                      // not be folded into it: whether the row is linked AT ALL
                      // without being a mutually-linked transfer.
                      //
                      // The name is a SHORTHAND, not a claim. Read it as "linked
                      // but not reciprocally". The one-way reconciliation match
                      // (reconciliation_service._apply_match writes
                      // `linked_transaction_id` on one leg only, so
                      // `linked_account_name` stays null) is the common producer
                      // but NOT the only one: a self-linked row (linked == id,
                      // corrupt but real), a cross-org link, and a chain A->B->C
                      // where the partner links onward all land here too. None of
                      // those was touched by reconciliation, so the UI copy must
                      // never say reconciliation caused it.
                      //
                      // Whatever the cause, `transaction_service._link_pair`
                      // invariant 7 rejects the row with "Expense leg is already
                      // linked", so offering "Mark transfer" on it is an
                      // affordance the server always refuses. Gate the offer on
                      // link-ness, not on transfer-ness.
                      //
                      // TRAP: this flag is only sound because every row here comes
                      // from list_transactions(collapse_transfers=true), the one
                      // caller that eager-loads Transaction.linked_transaction.
                      // transaction_service._load_opts() omits that selectinload,
                      // so GET /transactions/{id} and PUT responses carry
                      // linked_account_name: null even for a GENUINE transfer leg.
                      // Splicing such a single-row response into `transactions`
                      // would stamp a false "Matched" badge on both legs of a real
                      // transfer. Today only `recurring_id` is spliced, so it is
                      // safe; widen that splice and this flag breaks first.
                      const isReconcileMatched =
                        tx.linked_transaction_id != null && !isPairedTransfer;
                      // Direction comes from `type`, never from which leg
                      // survived the collapse: pair_existing_transactions and
                      // convert_and_create_leg link arbitrary rows, so the
                      // income leg can hold the lower id and the arrow used to
                      // render destination → source.
                      const [fromAcct, toAcct] = tx.type === "expense"
                        ? [tx.account_name, tx.linked_account_name]
                        : [tx.linked_account_name, tx.account_name];
                      const isTarget = targetTransactionId === tx.id;
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
                              <label htmlFor={`edit-cat-${tx.id}`} className={label}>{isPairedTransfer ? "Transfer category" : "Category"}</label>
                              <CategorySelect aria-label={isPairedTransfer ? "Transfer category" : "Category"} aria-describedby={isPairedTransfer ? `edit-cat-${tx.id}-help` : undefined} id={`edit-cat-${tx.id}`} categories={categories} value={editCategoryId} onChange={setEditCategoryId} filterType={isPairedTransfer ? undefined : editType} typeFilter={isPairedTransfer ? "BOTH" : undefined} className={`text-sm ${input}`} onCategoryCreated={(cat) => mutateCategories((prev) => [...(prev ?? []), cat], { revalidate: false })} />
                              {isPairedTransfer && (
                                <p id={`edit-cat-${tx.id}-help`} className="mt-1 text-xs text-text-secondary">Transfers only accept categories that work for both income and expense (for example, Transfer).</p>
                              )}
                              {categorizeAi?.entitled && !editPartner ? (
                                <div className="mt-1">
                                  {categorizeAi.configured ? (
                                    <span className="inline-flex items-center gap-1">
                                      <SuggestCategoryButton
                                        transactionId={tx.id}
                                        onSuggested={(s) => setEditCategoryId(s.category_id)}
                                        testIdPrefix={`ai-suggest-${tx.id}`}
                                      />
                                      <HelpTooltip k="ai.categorize" />
                                    </span>
                                  ) : (
                                    <SetUpAiCta
                                      role={role}
                                      className="text-xs text-text-secondary underline hover:text-text-primary"
                                    />
                                  )}
                                </div>
                              ) : null}
                            </div>
                            <div>
                              <label htmlFor={`edit-status-${tx.id}`} className={label}>Status</label>
                              <select id={`edit-status-${tx.id}`} aria-label="Status" value={editStatus} onChange={(e) => setEditStatus(e.target.value as "settled" | "pending")} className={`text-sm ${input}`}>
                                <option value="settled">Settled</option>
                                <option value="pending">Pending</option>
                              </select>
                            </div>
                            <div>
                              {editPartner ? (
                                <span className={label}>Type</span>
                              ) : (
                                <label htmlFor={`edit-type-${tx.id}`} className={label}>Type</label>
                              )}
                              {editPartner ? (
                                <span
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
                            <div>
                              <label htmlFor={`edit-tags-${tx.id}`} className={label}>Tags</label>
                              <TagChipInput
                                id={`edit-tags-${tx.id}`}
                                value={editTags}
                                onChange={setEditTags}
                                categoryId={editCategoryId}
                              />
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
                          {/* Promote-to-recurring (L3.12). Hidden for ANY linked
                              row; static chip when the row is already recurring.

                              TBD-295: `!editPartner` alone is not the gate.
                              `editPartner` is only hydrated for a MUTUAL pair
                              (startEdit's own TBD-268 gate), so it is null on a
                              reconcile-matched row and the checkbox rendered
                              there -- offering an action `promote_to_recurring`
                              refuses. Gate on the RAW column, matching the
                              server guard exactly. DESKTOP slot; the mobile
                              twin below carries the same gate. */}
                          {!editPartner && tx.linked_transaction_id === null && (
                            <div className="mt-3" data-testid={`edit-recurring-row-${tx.id}`}>
                              {tx.recurring_id !== null ? (
                                <div className="flex flex-col gap-1">
                                  <span
                                    className="inline-flex w-fit items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] text-text-muted"
                                    data-testid={`edit-recurring-chip-${tx.id}`}
                                  >
                                    Recurring
                                  </span>
                                  <p
                                    className="text-[11px] text-text-muted"
                                    data-testid={`edit-recurring-sync-hint-${tx.id}`}
                                  >
                                    Editing the name or category also updates this
                                    recurring series and its upcoming occurrences.
                                  </p>
                                </div>
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
                                      {/* TBD-301: no `min`. See handleSaveEdit
                                          -- the frontier bound is the org's
                                          cycle start, not today, so a
                                          today-floored picker greys out legal
                                          dates. Mirrored in the mobile card. */}
                                      <input
                                        aria-label="Next due date"
                                        type="date"
                                        value={editRecNextDue}
                                        onChange={(e) => setEditRecNextDue(e.target.value)}
                                        className={`text-[11px] !w-40 ${input}`}
                                      />
                                      {/* TBD-275. Blank = repeats indefinitely,
                                          which every promote did before this
                                          field existed. */}
                                      <input
                                        aria-label="Number of payments"
                                        // type="text": a number input coerces
                                        // unparseable text to "", silently
                                        // turning a counted plan open-ended.
                                        type="text"
                                        inputMode="numeric"
                                        placeholder="Payments (optional)"
                                        value={editRecOccurrenceCount}
                                        onChange={(e) =>
                                          setEditRecOccurrenceCount(e.target.value)
                                        }
                                        data-testid={`edit-recurring-count-${tx.id}`}
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
                            <button onClick={handleSaveEdit} className={btnPrimary}>Save</button>
                            <button onClick={closeEdit} className="min-h-[44px] rounded-md border border-border px-4 text-sm text-text-secondary hover:bg-surface-raised">Cancel</button>
                          </div>
                        </div>
                      ) : (
                        <div
                          key={tx.id}
                          ref={isTarget ? targetDesktopRowRef : null}
                          id={`transaction-${tx.id}`}
                          data-testid={`tx-row-desktop-${tx.id}`}
                          className={`grid grid-cols-12 items-center gap-4 px-6 py-3 transition-colors hover:bg-surface-raised ${
                            tx.status === "pending"
                              ? "[&>*:not(.tx-status-cell)]:opacity-60"
                              : ""
                          } ${isTarget ? "bg-accent-dim ring-2 ring-accent ring-inset" : ""}`}
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
                          <span className="col-span-1 text-sm tabular-nums text-text-secondary">
                            {tx.date}
                          </span>
                          {/* Settled date column (effective-date consistency).
                              The operator requires the settled date to be
                              visible wherever a transaction renders, so every
                              row shows it explicitly: the settled date when
                              set, or an em-dash placeholder when still pending
                              / unsettled. */}
                          <span
                            className="col-span-1 text-sm tabular-nums text-text-secondary"
                            data-testid={`settled-date-${tx.id}`}
                          >
                            {tx.settled_date ?? "—"}
                          </span>
                          <span className="col-span-2 flex flex-col text-sm text-text-primary">
                            <span>{tx.description}</span>
                            {tx.tags && tx.tags.length > 0 && (
                              <span className="mt-0.5 flex flex-wrap gap-1" data-testid={`row-tags-${tx.id}`}>
                                {tx.tags.map((t) => (
                                  <span
                                    key={t.id}
                                    className="inline-flex items-center rounded bg-accent/10 px-1.5 py-0.5 text-[10px] text-text-secondary"
                                  >
                                    {t.name}
                                  </span>
                                ))}
                              </span>
                            )}
                            {/* TBD-289 introduced this indicator deliberately
                                NON-interactive, because what a matched row should
                                let a user *do* was still an open ruling.

                                ⚠ TBD-295 IS that ruling, and it reverses this
                                half: the badge now LINKS to the canonical twin.
                                The TBD-289 fence that pinned "not a button, not a
                                link" is rewritten in the same PR — a prior fence
                                encoding the half of the problem its own ticket
                                did not fix.

                                Two constraints survive from TBD-289:
                                * NO link on a self-linked row (`linked === id`):
                                  the target is this row, so the link would be a
                                  no-op that claims otherwise.
                                * The copy never says "reconciliation". The flag
                                  means "linked but not reciprocally", equally
                                  true of a self-linked, cross-org or chained row
                                  that reconciliation never touched.

                                `title` on a bare element is not an accessible
                                name, so the sr-only text carries the sentence
                                into the accessibility tree (PRODUCT.md WCAG 2.2
                                AA). An <a> is focusable, so the title now has a
                                keyboard path too — but the sr-only text stays:
                                it is what a screen reader actually announces. */}
                            {isReconcileMatched && (
                              <span className="mt-0.5 inline-flex">
                                {tx.linked_transaction_id === tx.id ? (
                                  <span
                                    className={badgeNeutral}
                                    data-testid={`matched-badge-${tx.id}`}
                                    title={MATCHED_BADGE_TITLE}
                                  >
                                    Matched
                                    <span className="sr-only">: {MATCHED_BADGE_SR}</span>
                                  </span>
                                ) : (
                                  /* TBD-289 shipped this badge NON-interactive,
                                     so the touch-target floor did not apply to
                                     it. Making it a link is what brings the
                                     floor in — and the floor did not arrive
                                     with it. Outer <a> = WCAG 2.5.8 hit area,
                                     inner span = lean badge visual, the same
                                     split the status pill in this row uses.
                                     Putting min-h on the badge itself would
                                     paint a 44px-tall grey block.

                                     `lg:min-h-0` matches the Edit / Mark
                                     transfer / Unlink controls in this SAME
                                     row rather than the status pill: the pill
                                     sits alone in a fixed cell where 44px is
                                     free, while this badge stacks UNDER the
                                     description and an unconditional floor
                                     grows every matched row at every desktop
                                     width. The mobile twin carries the floor
                                     unconditionally. */
                                  <Link
                                    href={`/transactions?transaction_id=${tx.linked_transaction_id}`}
                                    className="inline-flex min-h-[44px] items-center lg:min-h-0"
                                    data-testid={`matched-badge-${tx.id}`}
                                    title={MATCHED_BADGE_TITLE}
                                  >
                                    <span className={`${badgeNeutral} hover:text-text-primary`}>
                                      Matched
                                      <span className="sr-only">: {MATCHED_BADGE_SR}</span>
                                    </span>
                                  </Link>
                                )}
                              </span>
                            )}
                          </span>
                          <span className="col-span-2 text-sm text-text-secondary truncate">
                            {isPairedTransfer
                              ? <>{fromAcct} &rarr; {toAcct}</>
                              : tx.account_name}
                          </span>
                          <span className="col-span-1 text-sm text-text-secondary truncate">{tx.category_name}</span>
                          <span className="tx-status-cell col-span-1 text-center">
                            {isPairedTransfer ? (
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
                          <span className={`col-span-1 text-right text-sm font-medium tabular-nums ${isPairedTransfer ? "text-accent" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                            {isPairedTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                          </span>
                          <span className="col-span-2 flex flex-wrap justify-end gap-x-2 gap-y-1">
                            <button onClick={() => startEdit(tx)} aria-label={`Edit: ${tx.description}`} disabled={bulkDeleting} className="min-h-[44px] lg:min-h-0 whitespace-nowrap text-xs text-text-muted hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed">Edit</button>
                            {!isPairedTransfer && !isReconcileMatched && (
                              <button onClick={() => setMarkModalSource(tx)} aria-label={`Mark as transfer: ${tx.description}`} disabled={bulkDeleting} className="min-h-[44px] lg:min-h-0 whitespace-nowrap text-xs text-text-muted hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed">Mark transfer</button>
                            )}
                            {isPairedTransfer && (
                              <button onClick={() => openUnpairModal(tx)} aria-label={`Unlink transfer: ${tx.description}`} disabled={bulkDeleting} className="min-h-[44px] lg:min-h-0 whitespace-nowrap text-xs text-text-muted hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed">Unlink</button>
                            )}
                            <button onClick={() => setConfirmDeleteId(tx.id)} aria-label={`Delete: ${tx.description}`} disabled={bulkDeleting} className="min-h-[44px] lg:min-h-0 whitespace-nowrap text-xs text-text-muted hover:text-danger disabled:opacity-40 disabled:cursor-not-allowed">Delete</button>
                          </span>
                        </div>
                      );
                    })}
                    {transactions.length === 0 && (
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
                    {transactions.map((tx) => {
                      // See the desktop renderer above: ONE mutuality-verified
                      // transfer signal for every affordance, and direction
                      // from `type`, not from which leg survived the
                      // server-side collapse.
                      const isPairedTransfer = tx.linked_account_name != null;
                      // See the desktop renderer: separate flag, separate
                      // question. Linked-but-not-reciprocally (TBD-289) gates the
                      // "Mark transfer" offer the server always refuses;
                      // transfer-ness still gates every transfer *rendering*.
                      // The name is shorthand — reconciliation is the common
                      // producer, not the only one — and the flag depends on
                      // list_transactions eager-loading linked_transaction, which
                      // _load_opts() does not. Both traps are written out in full
                      // above the desktop definition.
                      const isReconcileMatched =
                        tx.linked_transaction_id != null && !isPairedTransfer;
                      const [fromAcct, toAcct] = tx.type === "expense"
                        ? [tx.account_name, tx.linked_account_name]
                        : [tx.linked_account_name, tx.account_name];
                      const isTarget = targetTransactionId === tx.id;
                      if (editingId === tx.id) {
                        return (
                          <article key={tx.id} className="flex flex-col gap-3 rounded-lg border border-border bg-surface-raised p-4">
                            {editPartner && (
                              <div className="text-xs text-accent" data-testid={`edit-mirror-notice-mobile-${tx.id}`}>
                                Editing a transfer leg. Changes to amount apply to both rows.
                              </div>
                            )}
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                              <div>
                                <label htmlFor={`edit-date-mobile-${tx.id}`} className={label}>Date</label>
                                <input id={`edit-date-mobile-${tx.id}`} aria-label="Date" type="date" value={editDate} onChange={(e) => setEditDate(e.target.value)} className={`text-sm ${input}`} />
                              </div>
                              <div>
                                <label htmlFor={`edit-desc-mobile-${tx.id}`} className={label}>Description</label>
                                <input id={`edit-desc-mobile-${tx.id}`} aria-label="Description" type="text" required value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className={`text-sm ${input}`} />
                              </div>
                              <div>
                                <label htmlFor={`edit-account-mobile-${tx.id}`} className={label}>Account</label>
                                <select
                                  id={`edit-account-mobile-${tx.id}`}
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
                                <label htmlFor={`edit-cat-mobile-${tx.id}`} className={label}>{isPairedTransfer ? "Transfer category" : "Category"}</label>
                                <CategorySelect aria-label={isPairedTransfer ? "Transfer category" : "Category"} aria-describedby={isPairedTransfer ? `edit-cat-mobile-${tx.id}-help` : undefined} id={`edit-cat-mobile-${tx.id}`} categories={categories} value={editCategoryId} onChange={setEditCategoryId} filterType={isPairedTransfer ? undefined : editType} typeFilter={isPairedTransfer ? "BOTH" : undefined} className={`text-sm ${input}`} onCategoryCreated={(cat) => mutateCategories((prev) => [...(prev ?? []), cat], { revalidate: false })} />
                                {isPairedTransfer && (
                                  <p id={`edit-cat-mobile-${tx.id}-help`} className="mt-1 text-xs text-text-secondary">Transfers only accept categories that work for both income and expense (for example, Transfer).</p>
                                )}
                                {categorizeAi?.entitled && !editPartner ? (
                                  <div className="mt-1">
                                    {categorizeAi.configured ? (
                                      <span className="inline-flex items-center gap-1">
                                        <SuggestCategoryButton
                                          transactionId={tx.id}
                                          onSuggested={(s) => setEditCategoryId(s.category_id)}
                                          testIdPrefix={`ai-suggest-mobile-${tx.id}`}
                                        />
                                        <HelpTooltip k="ai.categorize" />
                                      </span>
                                    ) : (
                                      <SetUpAiCta
                                        role={role}
                                        className="text-xs text-text-secondary underline hover:text-text-primary"
                                      />
                                    )}
                                  </div>
                                ) : null}
                              </div>
                              <div>
                                <label htmlFor={`edit-status-mobile-${tx.id}`} className={label}>Status</label>
                                <select id={`edit-status-mobile-${tx.id}`} aria-label="Status" value={editStatus} onChange={(e) => setEditStatus(e.target.value as "settled" | "pending")} className={`text-sm ${input}`}>
                                  <option value="settled">Settled</option>
                                  <option value="pending">Pending</option>
                                </select>
                              </div>
                              <div>
                                {editPartner ? (
                                  <span className={label}>Type</span>
                                ) : (
                                  <label htmlFor={`edit-type-mobile-${tx.id}`} className={label}>Type</label>
                                )}
                                {editPartner ? (
                                  <span
                                    aria-label="Type"
                                    title="Type is fixed for transfer legs."
                                    className={`text-sm flex items-center px-3 rounded border border-border bg-surface text-text-muted h-10`}
                                  >
                                    {editType === "expense" ? "Expense" : "Income"}
                                  </span>
                                ) : (
                                  <select id={`edit-type-mobile-${tx.id}`} aria-label="Type" value={editType} onChange={(e) => { setEditType(e.target.value as "income" | "expense"); setEditCategoryId(""); }} className={`text-sm ${input}`}>
                                    <option value="expense">Expense</option>
                                    <option value="income">Income</option>
                                  </select>
                                )}
                              </div>
                              <div className="sm:col-span-2">
                                <label htmlFor={`edit-amount-mobile-${tx.id}`} className={label}>Amount</label>
                                <input id={`edit-amount-mobile-${tx.id}`} aria-label="Amount" type="number" step="0.01" min="0.01" value={editAmount} onChange={(e) => setEditAmount(e.target.value)} className={`text-sm ${input}`} />
                              </div>
                              <div className="sm:col-span-2">
                                <label htmlFor={`edit-tags-mobile-${tx.id}`} className={label}>Tags</label>
                                <TagChipInput
                                  id={`edit-tags-mobile-${tx.id}`}
                                  value={editTags}
                                  onChange={setEditTags}
                                  categoryId={editCategoryId}
                                />
                              </div>
                              {editStatus === "pending" && (
                                <div className="sm:col-span-2" data-testid={`edit-settled-date-cell-mobile-${tx.id}`}>
                                  <label htmlFor={`edit-settled-mobile-${tx.id}`} className={label}>Expected settlement date</label>
                                  <input
                                    id={`edit-settled-mobile-${tx.id}`}
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
                            {/* Promote-to-recurring (L3.12) — MOBILE layout. Same
                                gate as the desktop slot above: hidden on ANY
                                linked row, static chip when already recurring.
                                TBD-295 -- the raw column, not `editPartner`. */}
                            {!editPartner && tx.linked_transaction_id === null && (
                              <div data-testid={`edit-recurring-row-mobile-${tx.id}`}>
                                {tx.recurring_id !== null ? (
                                  <div className="flex flex-col gap-1">
                                    <span
                                      className="inline-flex w-fit items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-xs text-text-muted"
                                      data-testid={`edit-recurring-chip-mobile-${tx.id}`}
                                    >
                                      Recurring
                                    </span>
                                    <p
                                      className="text-[11px] text-text-muted"
                                      data-testid={`edit-recurring-sync-hint-mobile-${tx.id}`}
                                    >
                                      Editing the name or category also updates this
                                      recurring series and its upcoming occurrences.
                                    </p>
                                  </div>
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
                                          <label htmlFor={`edit-rec-frequency-mobile-${tx.id}`} className={label}>Frequency</label>
                                          <select
                                            id={`edit-rec-frequency-mobile-${tx.id}`}
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
                                          <label htmlFor={`edit-rec-nextdue-mobile-${tx.id}`} className={label}>Next due date</label>
                                          {/* TBD-301: no `min`, matching the
                                              desktop row. */}
                                          <input
                                            id={`edit-rec-nextdue-mobile-${tx.id}`}
                                            aria-label="Next due date"
                                            type="date"
                                            value={editRecNextDue}
                                            onChange={(e) => setEditRecNextDue(e.target.value)}
                                            className={`text-sm ${input}`}
                                          />
                                        </div>
                                        {/* TBD-275. Blank = repeats
                                            indefinitely. */}
                                        <div>
                                          <label htmlFor={`edit-rec-count-mobile-${tx.id}`} className={label}>Number of payments</label>
                                          <input
                                            id={`edit-rec-count-mobile-${tx.id}`}
                                            aria-label="Number of payments"
                                            type="text"
                                            inputMode="numeric"
                                            placeholder="Optional"
                                            value={editRecOccurrenceCount}
                                            onChange={(e) =>
                                              setEditRecOccurrenceCount(e.target.value)
                                            }
                                            data-testid={`edit-recurring-count-mobile-${tx.id}`}
                                            className={`text-sm ${input}`}
                                          />
                                          <p className="mt-1 text-[10px] text-text-muted">
                                            Counting this one. Leave blank to
                                            repeat indefinitely.
                                          </p>
                                        </div>
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            )}
                            <div className="flex flex-wrap gap-2 pt-2 border-t border-border-subtle">
                              <button onClick={handleSaveEdit} className={btnPrimary}>Save</button>
                              <button onClick={closeEdit} className="min-h-[44px] px-4 rounded-md border border-border text-sm text-text-secondary">Cancel</button>
                            </div>
                          </article>
                        );
                      }
                      return (
                        <article
                          key={tx.id}
                          ref={isTarget ? targetMobileRowRef : null}
                          id={`transaction-mobile-${tx.id}`}
                          data-testid={`tx-row-mobile-${tx.id}`}
                          className={`flex flex-col gap-2 rounded-lg border border-border bg-surface p-4 ${
                            isTarget ? "bg-accent-dim ring-2 ring-accent" : ""
                          }`}
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
                              {tx.tags && tx.tags.length > 0 && (
                                <div
                                  className="mt-0.5 flex flex-wrap gap-1"
                                  data-testid={`row-tags-mobile-${tx.id}`}
                                >
                                  {tx.tags.map((t) => (
                                    <span
                                      key={t.id}
                                      className="inline-flex items-center rounded bg-accent/10 px-1.5 py-0.5 text-[10px] text-text-secondary"
                                    >
                                      {t.name}
                                    </span>
                                  ))}
                                </div>
                              )}
                              <div className="mt-0.5 text-xs text-text-muted tabular-nums">
                                {tx.date} · {isPairedTransfer ? <>{fromAcct} &rarr; {toAcct}</> : tx.account_name}
                              </div>
                              {/* Settled date always surfaced on the mobile card
                                  too (effective-date consistency): the operator
                                  requires it visible wherever a transaction
                                  renders. Settled date when set, em-dash when
                                  still pending / unsettled. */}
                              <div
                                className="mt-0.5 text-[10px] text-text-muted tabular-nums"
                                data-testid={`settled-date-mobile-${tx.id}`}
                              >
                                Settled {tx.settled_date ?? "—"}
                              </div>
                              {/* TBD-295: mobile twin of the desktop matched
                                  indicator, now a LINK to the canonical twin —
                                  see the full note above the desktop slot,
                                  including why a self-linked row keeps the inert
                                  span. `title` is dead weight on touch, so the
                                  sr-only text is the only path the explanation
                                  has to a screen-reader user here. */}
                              {isReconcileMatched && (
                                <div className="mt-1">
                                  {tx.linked_transaction_id === tx.id ? (
                                    <span
                                      className={badgeNeutral}
                                      data-testid={`matched-badge-mobile-${tx.id}`}
                                      title={MATCHED_BADGE_TITLE}
                                    >
                                      Matched
                                      <span className="sr-only">: {MATCHED_BADGE_SR}</span>
                                    </span>
                                  ) : (
                                    /* Touch-target floor, unconditional here:
                                       this renderer IS the touch surface. See
                                       the desktop twin for why the split
                                       (hit area outside, badge visual inside)
                                       rather than min-h on the badge. */
                                    <Link
                                      href={`/transactions?transaction_id=${tx.linked_transaction_id}`}
                                      className="inline-flex min-h-[44px] items-center"
                                      data-testid={`matched-badge-mobile-${tx.id}`}
                                      title={MATCHED_BADGE_TITLE}
                                    >
                                      <span className={`${badgeNeutral} hover:text-text-primary`}>
                                        Matched
                                        <span className="sr-only">: {MATCHED_BADGE_SR}</span>
                                      </span>
                                    </Link>
                                  )}
                                </div>
                              )}
                            </div>
                            <div className={`shrink-0 text-right text-sm font-semibold tabular-nums ${isPairedTransfer ? "text-accent" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                              {isPairedTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
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
                            {isPairedTransfer ? (
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
                            {!isPairedTransfer && !isReconcileMatched && (
                              <button
                                onClick={() => setMarkModalSource(tx)}
                                aria-label={`Mark as transfer: ${tx.description}`}
                                disabled={bulkDeleting}
                                className="min-h-[44px] px-3 rounded-md border border-border text-sm text-text-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                              >
                                Mark as transfer…
                              </button>
                            )}
                            {isPairedTransfer && (
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
                    {transactions.length === 0 && (
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

          {(total > pageSize || page > 0) && (
            <div className="mt-4">
              <Pagination
                page={page + 1}
                pageSize={pageSize}
                total={total}
                onPageChange={(n) => setPage(n - 1)}
                onPageSizeChange={changePageSize}
              />
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
      <BatchEditModal
        open={showBatchEdit}
        count={selectedIds.size}
        categories={categories}
        accounts={accounts}
        submitting={batchEditing}
        onSubmit={handleBatchEdit}
        onCancel={() => setShowBatchEdit(false)}
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
