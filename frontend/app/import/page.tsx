"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Fragment, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import AppShell from "@/components/AppShell";
import CategorySelect from "@/components/ui/CategorySelect";
import Spinner from "@/components/ui/Spinner";
import ImportMarkAsTransferModal from "@/components/transactions/ImportMarkAsTransferModal";
import { input, label, btnPrimary, btnSecondary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import type {
  Account,
  Category,
  ImportConfirmResponse,
  ImportConfirmRow,
  ImportPreviewResponse,
  ImportPreviewRow,
} from "@/lib/types";


type Step = "upload" | "preview" | "results";

// Transfer-pill UI state (parallel map keyed by row_number — UI only,
// independent of the confirm-payload row state).
type TransferUiState = {
  panelOpen: boolean;
  pairAccepted: boolean;
  selectedCandidateId: number | null;
  dropAccepted: boolean;
  // Manual "Mark as transfer..." choice for un-flagged rows. When set, the
  // row will be confirmed as create_transfer_pair with this as partner.
  markTransferDestAccountId: number | null;
};

/**
 * Build a confirm-payload row by combining the existing row state with the
 * preview detector outputs and per-row transfer-pill UI state. Pure function
 * (no side effects) so it can be unit-tested or batch-mapped.
 *
 * Action precedence (per spec §4.6):
 *   1. is_duplicate_of_linked_leg + dropAccepted → "drop_as_duplicate"
 *   2. transfer_match_action != "none" + user-confirmed pair → "pair_with_existing"
 *   3. otherwise → "create"
 *
 * Notes:
 * - skip semantics (UI-only "don't import this row") is unchanged. drop_as_duplicate
 *   is server-side: the row is still submitted, backend skips it after revalidation.
 * - When dropAccepted is FALSE on an is_duplicate_of_linked_leg row, we fall
 *   through to the pair / create branches: the user implicitly chose "Keep both",
 *   meaning the row imports as a regular transaction (or pairs if they also
 *   accepted a pair candidate).
 */
function buildConfirmRow(
  rowState: ImportConfirmRow,
  preview: ImportPreviewRow,
  ui: TransferUiState,
): ImportConfirmRow {
  const isDup = preview.is_duplicate_of_linked_leg;
  const matchAction = preview.transfer_match_action;

  if (isDup && ui.dropAccepted) {
    return {
      ...rowState,
      action: "drop_as_duplicate",
      duplicate_of_transaction_id: preview.duplicate_candidate?.id ?? null,
      pair_with_transaction_id: null,
      transfer_category_id: null,
      recategorize: undefined,
    };
  }

  const hasPairChoice =
    matchAction === "pair_with" || matchAction === "suggest_pair"
      ? ui.pairAccepted
      : matchAction === "choose_candidate"
      ? ui.selectedCandidateId !== null
      : false;

  if (matchAction !== "none" && hasPairChoice) {
    const partnerId =
      matchAction === "choose_candidate"
        ? ui.selectedCandidateId
        : preview.pair_with_transaction_id ?? null;
    return {
      ...rowState,
      action: "pair_with_existing",
      pair_with_transaction_id: partnerId,
      duplicate_of_transaction_id: null,
      transfer_category_id: null,
      recategorize: true,
    };
  }

  // Manual "Mark as transfer..." — the user flagged an un-detected row
  // and picked a destination account. Backend creates the CSV leg + a
  // synthetic partner leg on the chosen account, atomically.
  if (ui.markTransferDestAccountId !== null) {
    return {
      ...rowState,
      action: "create_transfer_pair",
      partner_account_id: ui.markTransferDestAccountId,
      pair_with_transaction_id: null,
      duplicate_of_transaction_id: null,
      transfer_category_id: null,
      recategorize: true,
    };
  }

  return {
    ...rowState,
    action: "create",
    pair_with_transaction_id: null,
    duplicate_of_transaction_id: null,
    transfer_category_id: null,
    recategorize: undefined,
  };
}

export default function ImportPage() {
  return (
    <Suspense>
      <ImportPageContent />
    </Suspense>
  );
}

function ImportPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // ── Shared data ──────────────────────────────────────────────────────────
  const { data: accounts } = useSWR<Account[]>("accounts", () => apiFetch<Account[]>("/api/v1/accounts"));
  const { data: categories, mutate: mutateCategories } = useSWR<Category[]>("categories", () => apiFetch<Category[]>("/api/v1/categories"));

  const activeAccounts = useMemo(() => accounts?.filter((a) => a.is_active) ?? [], [accounts]);
  const defaultAccount = useMemo(() => activeAccounts.find((a) => a.is_default), [activeAccounts]);

  // Group categories by type for the dropdown
  const expenseCategories = useMemo(
    () => categories?.filter((c) => c.type === "expense" || c.type === "both") ?? [],
    [categories],
  );
  const incomeCategories = useMemo(
    () => categories?.filter((c) => c.type === "income" || c.type === "both") ?? [],
    [categories],
  );

  // ── Step state ───────────────────────────────────────────────────────────
  const [step, setStep] = useState<Step>("upload");
  const [errorMsg, setErrorMsg] = useState("");
  const [loading, setLoading] = useState(false);

  // Upload step
  const [file, setFile] = useState<File | null>(null);
  const [accountId, setAccountId] = useState<number | "">("");

  // Preview step
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [rowStates, setRowStates] = useState<ImportConfirmRow[]>([]);
  const [defaultCategoryId, setDefaultCategoryId] = useState<number | "">("");

  // Transfer-pill UI state (parallel map keyed by row_number — UI only).
  // Confirm-payload mapping happens via buildConfirmRow at submit time.
  const [transferUi, setTransferUi] = useState<Record<number, TransferUiState>>({});

  // Review pairings filter — when ON, table only renders rows with a
  // transfer detector match (Detector 1 or Detector 2).
  const [reviewPairingsOnly, setReviewPairingsOnly] = useState(false);

  // Results step
  const [results, setResults] = useState<ImportConfirmResponse | null>(null);

  // Pre-select account from URL param or default
  useEffect(() => {
    const paramId = searchParams.get("account");
    if (paramId) {
      setAccountId(Number(paramId));
    } else if (defaultAccount && accountId === "") {
      setAccountId(defaultAccount.id);
    }
  }, [defaultAccount, searchParams, accountId]);

  // ── Upload handler ───────────────────────────────────────────────────────
  const handleUpload = useCallback(async () => {
    if (!file || accountId === "") return;
    setErrorMsg("");
    setLoading(true);

    const formData = new FormData();
    formData.append("file", file);
    formData.append("account_id", accountId.toString());

    try {
      const data = await apiFetch<ImportPreviewResponse>("/api/v1/import/preview", {
        method: "POST",
        body: formData,
      });
      setPreview(data);

      // Initialize row states from preview
      setRowStates(
        data.rows.map((r: ImportPreviewRow) => ({
          row_number: r.row_number,
          date: r.date,
          description: r.description,
          amount: r.amount,
          type: r.type,
          category_id: r.suggested_category_id ?? null,
          skip: r.is_duplicate, // pre-skip duplicates
          action: "create" as const,
          suggested_category_id: r.suggested_category_id ?? null,
          suggestion_source: r.suggestion_source ?? null,
        })),
      );

      // Initialize transfer-pill UI state per row from preview detectors.
      const ui: Record<number, TransferUiState> = {};
      data.rows.forEach((r: ImportPreviewRow) => {
        ui[r.row_number] = {
          panelOpen: false,
          // pair_with (same_day) defaults checked. suggest_pair (near_date)
          // defaults unchecked. choose_candidate stays unchecked until user picks.
          pairAccepted: r.transfer_match_action === "pair_with",
          selectedCandidateId: null,
          dropAccepted: r.is_duplicate_of_linked_leg, // default Drop
          markTransferDestAccountId: null,
        };
      });
      setTransferUi(ui);

      setStep("preview");
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Failed to parse file"));
    } finally {
      setLoading(false);
    }
  }, [file, accountId]);

  // ── Confirm handler ──────────────────────────────────────────────────────
  const handleConfirm = useCallback(async () => {
    if (!preview || defaultCategoryId === "") return;
    setErrorMsg("");
    setLoading(true);

    // Map every row through buildConfirmRow so action / partner-id /
    // duplicate-id reflect the user's per-row transfer choices.
    const previewByRow = new Map(preview.rows.map((r) => [r.row_number, r]));
    const defaultUi: TransferUiState = {
      panelOpen: false,
      pairAccepted: false,
      selectedCandidateId: null,
      dropAccepted: false,
      markTransferDestAccountId: null,
    };
    const payloadRows = rowStates.map((rs) => {
      const pv = previewByRow.get(rs.row_number);
      if (!pv) return rs;
      const ui = transferUi[rs.row_number] ?? defaultUi;
      return buildConfirmRow(rs, pv, ui);
    });

    try {
      const data = await apiFetch<ImportConfirmResponse>("/api/v1/import/confirm", {
        method: "POST",
        body: JSON.stringify({
          account_id: preview.account_id,
          default_category_id: defaultCategoryId,
          rows: payloadRows,
        }),
      });
      setResults(data);
      setStep("results");
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Import failed"));
    } finally {
      setLoading(false);
    }
  }, [preview, defaultCategoryId, rowStates, transferUi]);

  // ── Row update helpers ───────────────────────────────────────────────────
  const updateRow = useCallback((rowNum: number, patch: Partial<ImportConfirmRow>) => {
    setRowStates((prev) =>
      prev.map((r) => (r.row_number === rowNum ? { ...r, ...patch } : r)),
    );
  }, []);

  const updateTransferUi = useCallback((rowNum: number, patch: Partial<TransferUiState>) => {
    setTransferUi((prev) => ({
      ...prev,
      [rowNum]: { ...(prev[rowNum] ?? {
        panelOpen: false,
        pairAccepted: false,
        selectedCandidateId: null,
        dropAccepted: false,
        markTransferDestAccountId: null,
      }), ...patch },
    }));
  }, []);

  // Modal state — when set, opens ImportMarkAsTransferModal for that row.
  const [markTransferModalRow, setMarkTransferModalRow] = useState<ImportPreviewRow | null>(null);

  const activeRows = rowStates.filter((r) => !r.skip);
  const skipCount = rowStates.filter((r) => r.skip).length;

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <AppShell>
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className={pageTitle}>Import Transactions</h1>
        <button onClick={() => router.back()} className={btnSecondary}>
          Back
        </button>
      </div>

      {errorMsg && <div className={errorCls}>{errorMsg}</div>}

      {categories === undefined && (
        <div className={card}>
          <Spinner />
        </div>
      )}

      {categories?.length === 0 && (
        <div className={`${card} p-10 text-center`}>
          <p className="text-text-secondary">No categories yet.</p>
          <p className="mt-2 text-sm text-text-muted">
            Add at least one category before importing transactions.
          </p>
          <Link
            href="/categories"
            className={btnPrimary + " mt-4 inline-flex min-h-[44px] items-center sm:min-h-0"}
          >
            Go to Categories
          </Link>
        </div>
      )}

      {/* ── Step 1: Upload ──────────────────────────────────────────────── */}
      {step === "upload" && categories && categories.length > 0 && (
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Upload CSV File</h2>
          </div>
          <div className="space-y-4 p-6">
            <div>
              <label className={label}>Target Account</label>
              <select
                value={accountId}
                onChange={(e) => setAccountId(e.target.value === "" ? "" : Number(e.target.value))}
                className={input}
              >
                <option value="">Select account...</option>
                {activeAccounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.currency})
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className={label}>CSV File</label>
              <input
                type="file"
                accept=".csv"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="block w-full text-sm text-text-secondary file:mr-4 file:rounded-md file:border-0 file:bg-accent file:px-4 file:py-2 file:text-sm file:font-medium file:text-accent-text hover:file:bg-accent-hover"
              />
            </div>
            <button
              onClick={handleUpload}
              disabled={!file || accountId === "" || loading}
              className={btnPrimary + " min-h-[44px] w-full sm:min-h-0 sm:w-auto"}
            >
              {loading ? "Parsing..." : "Upload & Preview"}
            </button>
          </div>
        </div>
      )}

      {/* ── Step 2: Preview ─────────────────────────────────────────────── */}
      {step === "preview" && preview && categories && categories.length > 0 && (() => {
        // Account selected at upload time. Drives the eligible-for-manual-mark
        // check (need at least one OTHER same-currency account).
        const currentImportAccount = accounts?.find((a) => a.id === preview.account_id);
        const eligibleForManualMark =
          !!currentImportAccount &&
          (accounts?.filter(
            (a) =>
              a.id !== preview.account_id &&
              a.currency === currentImportAccount.currency &&
              a.is_active,
          ).length ?? 0) > 0;

        // Lookup map for rendering "Will create transfer to <name>" pill.
        const accountsById = new Map((accounts ?? []).map((a) => [a.id, a]));

        // Show the Transfer column when ANY row has a detector hit OR when
        // manual marking is possible (so even on a no-detector-hit import the
        // column appears with per-row "Mark as transfer..." buttons).
        const hasAnyTransferState =
          preview.auto_paired_count +
            preview.suggested_pair_count +
            preview.multi_candidate_count +
            preview.duplicate_of_linked_count >
            0 || eligibleForManualMark;
        return (
        <div className="space-y-4">
          {/* Summary bar */}
          <div className={card}>
            <div className="flex flex-col gap-3 px-6 py-4 text-sm sm:flex-row sm:flex-wrap sm:items-center sm:gap-4">
              <span className="font-medium text-text-primary">{preview.file_name}</span>
              <span className="text-text-muted">{preview.total_rows} transactions</span>
              {preview.duplicate_count > 0 && (
                <span className="rounded bg-warning-dim px-2 py-0.5 text-warning">
                  {preview.duplicate_count} duplicates
                </span>
              )}
              {preview.auto_paired_count > 0 && (
                <span className="rounded bg-accent/10 px-2 py-0.5 text-accent">
                  {preview.auto_paired_count} auto-paired
                </span>
              )}
              {preview.suggested_pair_count > 0 && (
                <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-800">
                  {preview.suggested_pair_count} possible transfers
                </span>
              )}
              {preview.multi_candidate_count > 0 && (
                <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-800">
                  {preview.multi_candidate_count} need a pick
                </span>
              )}
              {preview.duplicate_of_linked_count > 0 && (
                <span className="rounded bg-rose-100 px-2 py-0.5 text-rose-800">
                  {preview.duplicate_of_linked_count} dup of linked leg
                </span>
              )}
            </div>
          </div>

          {/* Default category */}
          <div className={card}>
            <div className="p-6">
              <label className={label}>
                Default Category (applied to rows without a specific category)
              </label>
              <select
                value={defaultCategoryId}
                onChange={(e) => setDefaultCategoryId(e.target.value === "" ? "" : Number(e.target.value))}
                className={input + " max-w-sm"}
              >
                <option value="">Select default category...</option>
                {categories?.filter((c) => !c.parent_id).map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Review pairings filter toggle */}
          <div className={card}>
            <div className="flex flex-col gap-2 px-6 py-3 text-sm sm:flex-row sm:items-center sm:gap-4">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={reviewPairingsOnly}
                  onChange={(e) => setReviewPairingsOnly(e.target.checked)}
                  className="rounded border-border"
                  data-testid="review-pairings-toggle"
                />
                <span className="font-medium text-text-primary">Review pairings only</span>
              </label>
              <span className="text-text-muted">
                {preview.auto_paired_count} auto-paired ·{" "}
                {preview.suggested_pair_count} suggested ·{" "}
                {preview.multi_candidate_count} multi-candidate ·{" "}
                {preview.duplicate_of_linked_count} duplicate
              </span>
            </div>
          </div>

          {/* Preview table */}
          <div className={card + " overflow-x-auto"}>
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-border text-left">
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Skip</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Date</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Description</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Amount</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Type</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Category</th>
                  {hasAnyTransferState && (
                    <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Transfer</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((previewRow, idx) => {
                  const rowState = rowStates[idx];
                  if (!rowState) return null;

                  // Apply Review pairings filter — when ON, only render rows
                  // with a transfer detector match (Detector 1 or Detector 2)
                  // OR a manual "Mark as transfer..." selection in the
                  // per-row UI state.
                  const manualMarkDestId =
                    transferUi[previewRow.row_number]?.markTransferDestAccountId ?? null;
                  if (
                    reviewPairingsOnly &&
                    previewRow.transfer_match_action === "none" &&
                    !previewRow.is_duplicate_of_linked_leg &&
                    manualMarkDestId === null
                  ) {
                    return null;
                  }

                  const catOptions = rowState.type === "income" ? incomeCategories : expenseCategories;
                  const isDup = previewRow.is_duplicate;

                  let rowBg = "";
                  if (rowState.skip) rowBg = "opacity-40";
                  else if (isDup) rowBg = "bg-warning-dim";

                  const ui = transferUi[previewRow.row_number] ?? {
                    panelOpen: false,
                    pairAccepted: false,
                    selectedCandidateId: null,
                    dropAccepted: false,
                    markTransferDestAccountId: null,
                  };

                  // Pill rendering driven by detector outputs. Detector 1
                  // (is_duplicate_of_linked_leg) takes precedence over detector 2.
                  let pill: { text: string; classes: string } | null = null;
                  if (previewRow.is_duplicate_of_linked_leg) {
                    pill = {
                      text: "Drop as duplicate",
                      classes: "bg-rose-100 text-rose-800 hover:bg-rose-200",
                    };
                  } else if (previewRow.transfer_match_action === "pair_with") {
                    pill = {
                      text: "Pair as transfer",
                      classes: "bg-accent/15 text-accent hover:bg-accent/25",
                    };
                  } else if (previewRow.transfer_match_action === "suggest_pair") {
                    const cand = previewRow.transfer_candidates[0];
                    const days = cand ? Math.abs(cand.date_diff_days) : 0;
                    pill = {
                      text: `Possible transfer (±${days} day${days === 1 ? "" : "s"})`,
                      classes: "bg-amber-100 text-amber-800 hover:bg-amber-200",
                    };
                  } else if (previewRow.transfer_match_action === "choose_candidate") {
                    pill = {
                      text: "Multiple candidates",
                      classes: "bg-amber-100 text-amber-800 hover:bg-amber-200",
                    };
                  }

                  // Determine table column count for panel-row colspan.
                  // Tracks whether the Transfer column is rendered.
                  const COL_COUNT = hasAnyTransferState ? 7 : 6;

                  return (
                    <Fragment key={previewRow.row_number}>
                      <tr className={`border-b border-border ${rowBg}`}>
                        <td className="px-4 py-2">
                          <input
                            type="checkbox"
                            checked={rowState.skip}
                            onChange={(e) => updateRow(previewRow.row_number, { skip: e.target.checked })}
                            className="rounded border-border"
                          />
                        </td>
                        <td className="px-4 py-2 tabular-nums text-text-secondary">{previewRow.date}</td>
                        <td className="max-w-[300px] truncate px-4 py-2 text-text-primary" title={previewRow.description}>
                          {previewRow.description}
                          {isDup && (
                            <span className="ml-2 text-xs text-warning">duplicate</span>
                          )}
                        </td>
                        <td className="px-4 py-2 tabular-nums font-medium">
                          <span className={previewRow.type === "income" ? "text-success" : "text-danger"}>
                            {previewRow.type === "income" ? "+" : "-"}{Number(previewRow.amount).toFixed(2)}
                          </span>
                        </td>
                        <td className="px-4 py-2 capitalize text-text-secondary">{previewRow.type}</td>
                        <td className="px-4 py-2">
                          {!rowState.skip && (
                            <div className="flex items-center">
                              <CategorySelect
                                id={`cat-${previewRow.row_number}`}
                                categories={catOptions}
                                value={rowState.category_id ?? ""}
                                onChange={(id) =>
                                  updateRow(previewRow.row_number, {
                                    category_id: id === "" ? null : id,
                                  })
                                }
                                filterType={previewRow.type === "income" ? "income" : "expense"}
                                className={input + " !w-48"}
                                onCategoryCreated={() => {
                                  void mutateCategories();
                                }}
                              />
                              {previewRow.suggestion_source === "org_rule" && (
                                <span
                                  className="ml-2 text-xs text-text-muted"
                                  data-testid="suggestion-badge"
                                >
                                  Auto · org rule
                                </span>
                              )}
                              {previewRow.suggestion_source === "shared_dictionary" && (
                                <span
                                  className="ml-2 text-xs text-text-muted"
                                  data-testid="suggestion-badge"
                                >
                                  Auto · shared
                                </span>
                              )}
                            </div>
                          )}
                        </td>
                        {hasAnyTransferState && (
                          <td className="px-4 py-2">
                            {pill ? (
                              <button
                                type="button"
                                onClick={() =>
                                  updateTransferUi(previewRow.row_number, { panelOpen: !ui.panelOpen })
                                }
                                className={`rounded-full px-2.5 py-1 text-xs font-medium transition-colors ${pill.classes}`}
                                aria-expanded={ui.panelOpen}
                                data-testid={`transfer-pill-${previewRow.row_number}`}
                              >
                                {pill.text}
                              </button>
                            ) : previewRow.transfer_match_action === "none" &&
                              !previewRow.is_duplicate_of_linked_leg ? (
                              ui.markTransferDestAccountId !== null ? (
                                <span
                                  className="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2.5 py-1 text-xs font-medium text-accent"
                                  data-testid={`mark-transfer-pill-${previewRow.row_number}`}
                                >
                                  <span>
                                    Will create transfer{" "}
                                    {previewRow.type === "expense" ? "to" : "from"}{" "}
                                    {accountsById.get(ui.markTransferDestAccountId)?.name ??
                                      "account"}
                                  </span>
                                  <button
                                    type="button"
                                    onClick={() =>
                                      updateTransferUi(previewRow.row_number, {
                                        markTransferDestAccountId: null,
                                      })
                                    }
                                    aria-label="Clear mark as transfer"
                                    className="ml-1 rounded text-accent hover:text-accent-hover"
                                    data-testid={`mark-transfer-clear-${previewRow.row_number}`}
                                  >
                                    x
                                  </button>
                                </span>
                              ) : eligibleForManualMark ? (
                                <button
                                  type="button"
                                  onClick={() => setMarkTransferModalRow(previewRow)}
                                  className="text-xs text-text-muted underline-offset-2 hover:text-accent hover:underline"
                                  data-testid={`mark-transfer-button-${previewRow.row_number}`}
                                >
                                  Mark as transfer...
                                </button>
                              ) : (
                                <span className="text-text-muted">—</span>
                              )
                            ) : (
                              <span className="text-text-muted">—</span>
                            )}
                          </td>
                        )}
                      </tr>

                      {pill && ui.panelOpen && (
                        <tr
                          className="border-b border-border bg-surface-2/50"
                          data-testid={`transfer-panel-${previewRow.row_number}`}
                        >
                          <td colSpan={COL_COUNT} className="px-6 py-3">
                            {/* Detector 1: matches an already-linked leg on this account → Drop. */}
                            {previewRow.is_duplicate_of_linked_leg && previewRow.duplicate_candidate && (
                              <div className="space-y-2 text-sm">
                                <div className="font-medium text-text-primary">Matches an existing linked leg</div>
                                <div className="text-text-secondary">
                                  {previewRow.duplicate_candidate.date} · {previewRow.duplicate_candidate.account_name} ·{" "}
                                  <span className="tabular-nums">
                                    {Number(previewRow.duplicate_candidate.amount).toFixed(2)}
                                  </span>{" "}
                                  · {previewRow.duplicate_candidate.description}
                                </div>
                                {previewRow.duplicate_candidate.existing_leg_is_imported === false && (
                                  <span className="inline-block rounded bg-violet-100 px-2 py-0.5 text-xs text-violet-800">
                                    Synthetic leg from convert-to-transfer
                                  </span>
                                )}
                                <label className="flex items-center gap-2">
                                  <input
                                    type="checkbox"
                                    checked={ui.dropAccepted}
                                    onChange={(e) =>
                                      updateTransferUi(previewRow.row_number, { dropAccepted: e.target.checked })
                                    }
                                    className="rounded border-border"
                                  />
                                  <span>{ui.dropAccepted ? "Drop this row" : "Keep both"}</span>
                                </label>
                              </div>
                            )}

                            {/* Detector 2: pair_with / suggest_pair (single candidate). */}
                            {!previewRow.is_duplicate_of_linked_leg &&
                              (previewRow.transfer_match_action === "pair_with" ||
                                previewRow.transfer_match_action === "suggest_pair") &&
                              previewRow.transfer_candidates[0] && (
                                <div className="space-y-2 text-sm">
                                  <div className="font-medium text-text-primary">
                                    {previewRow.transfer_match_action === "pair_with"
                                      ? "Same-day match found"
                                      : "Near-date match found"}
                                  </div>
                                  <div className="text-text-secondary">
                                    {previewRow.transfer_candidates[0].date} ·{" "}
                                    {previewRow.transfer_candidates[0].account_name} ·{" "}
                                    <span className="tabular-nums">
                                      {Number(previewRow.transfer_candidates[0].amount).toFixed(2)}
                                    </span>{" "}
                                    · {previewRow.transfer_candidates[0].description}
                                  </div>
                                  <label className="flex items-center gap-2">
                                    <input
                                      type="checkbox"
                                      checked={ui.pairAccepted}
                                      onChange={(e) =>
                                        updateTransferUi(previewRow.row_number, {
                                          pairAccepted: e.target.checked,
                                        })
                                      }
                                      className="rounded border-border"
                                    />
                                    <span>
                                      {ui.pairAccepted ? "Pair as transfer" : "Don't pair"}
                                    </span>
                                  </label>
                                </div>
                              )}

                            {/* Detector 2: choose_candidate (multi-candidate radio list). */}
                            {!previewRow.is_duplicate_of_linked_leg &&
                              previewRow.transfer_match_action === "choose_candidate" && (
                                <div className="space-y-2 text-sm">
                                  <div className="font-medium text-text-primary">
                                    Pick a candidate to pair with
                                  </div>
                                  <ul className="space-y-1">
                                    {previewRow.transfer_candidates.map((cand, candIdx) => {
                                      // Closest candidate (smallest |date_diff_days|, first in list)
                                      // is pre-highlighted via a subtle border, but NOT pre-selected.
                                      const isClosest = candIdx === 0;
                                      const isSelected = ui.selectedCandidateId === cand.id;
                                      return (
                                        <li key={cand.id}>
                                          <label
                                            className={`flex cursor-pointer items-center gap-2 rounded border px-2 py-1.5 ${
                                              isSelected
                                                ? "border-accent bg-accent/5"
                                                : isClosest
                                                ? "border-amber-300"
                                                : "border-border"
                                            }`}
                                          >
                                            <input
                                              type="radio"
                                              name={`cand-${previewRow.row_number}`}
                                              checked={isSelected}
                                              onChange={() =>
                                                updateTransferUi(previewRow.row_number, {
                                                  selectedCandidateId: cand.id,
                                                  pairAccepted: true,
                                                })
                                              }
                                            />
                                            <span className="text-text-secondary">
                                              {cand.date} · {cand.account_name} ·{" "}
                                              <span className="tabular-nums">
                                                {Number(cand.amount).toFixed(2)}
                                              </span>{" "}
                                              · {cand.description}
                                              {isClosest && (
                                                <span className="ml-2 text-xs text-amber-700">closest</span>
                                              )}
                                            </span>
                                          </label>
                                        </li>
                                      );
                                    })}
                                    <li>
                                      <label className="flex cursor-pointer items-center gap-2 rounded border border-border px-2 py-1.5">
                                        <input
                                          type="radio"
                                          name={`cand-${previewRow.row_number}`}
                                          checked={ui.selectedCandidateId === null && ui.pairAccepted === false}
                                          onChange={() =>
                                            updateTransferUi(previewRow.row_number, {
                                              selectedCandidateId: null,
                                              pairAccepted: false,
                                            })
                                          }
                                        />
                                        <span className="text-text-secondary">Skip — don't pair</span>
                                      </label>
                                    </li>
                                  </ul>
                                </div>
                              )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Actions */}
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:items-center sm:gap-4">
            {skipCount > 0 && (
              <span className="text-sm text-text-muted sm:order-3">{skipCount} skipped</span>
            )}
            <button
              onClick={() => { setStep("upload"); setPreview(null); setFile(null); }}
              className={btnSecondary + " min-h-[44px] w-full sm:order-2 sm:min-h-0 sm:w-auto"}
            >
              Start Over
            </button>
            <button
              onClick={handleConfirm}
              disabled={defaultCategoryId === "" || activeRows.length === 0 || loading}
              className={btnPrimary + " min-h-[44px] w-full sm:order-1 sm:min-h-0 sm:w-auto"}
            >
              {loading
                ? "Importing..."
                : `Import ${activeRows.length} transaction${activeRows.length === 1 ? "" : "s"}`}
            </button>
          </div>

          {/* Mark-as-transfer modal (manual flag for un-detected rows). */}
          {markTransferModalRow && currentImportAccount && (
            <ImportMarkAsTransferModal
              rowNumber={markTransferModalRow.row_number}
              rowDescription={markTransferModalRow.description}
              rowAmount={markTransferModalRow.amount}
              rowDate={markTransferModalRow.date}
              rowType={markTransferModalRow.type}
              importAccountId={preview.account_id}
              importAccountName={currentImportAccount.name}
              importAccountCurrency={currentImportAccount.currency}
              accounts={accounts ?? []}
              initialDestAccountId={
                transferUi[markTransferModalRow.row_number]?.markTransferDestAccountId ?? null
              }
              onConfirm={(destAccountId) => {
                updateTransferUi(markTransferModalRow.row_number, {
                  markTransferDestAccountId: destAccountId,
                });
                setMarkTransferModalRow(null);
              }}
              onCancel={() => setMarkTransferModalRow(null)}
            />
          )}
        </div>
        );
      })()}

      {/* ── Step 3: Results ─────────────────────────────────────────────── */}
      {step === "results" && results && (
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Import Complete</h2>
          </div>
          <div className="space-y-2 p-6 text-sm">
            <p className="text-success">
              {results.imported_count} transaction{results.imported_count === 1 ? "" : "s"} imported
            </p>
            {results.paired_count > 0 && (
              <p className="text-text-muted">
                {results.paired_count} paired as transfer{results.paired_count === 1 ? "" : "s"}
              </p>
            )}
            {results.dropped_duplicate_count > 0 && (
              <p className="text-text-muted">
                {results.dropped_duplicate_count} dropped as duplicate of existing transfer leg
              </p>
            )}
            {results.skipped_count > 0 && (
              <p className="text-text-muted">{results.skipped_count} skipped</p>
            )}
            {results.error_count > 0 && (
              <div>
                <p className="font-medium text-danger">{results.error_count} errors:</p>
                <ul className="ml-4 mt-1 list-disc text-danger">
                  {results.errors.map((e) => (
                    <li key={e.row_number}>Row {e.row_number}: {e.error}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
          <div className="flex flex-col-reverse gap-2 border-t border-border px-6 py-4 sm:flex-row sm:gap-4">
            <button
              onClick={() => { setStep("upload"); setPreview(null); setResults(null); setFile(null); }}
              className={btnSecondary + " min-h-[44px] w-full sm:order-2 sm:min-h-0 sm:w-auto"}
            >
              Import Another File
            </button>
            <button
              onClick={() => router.push("/transactions")}
              className={btnPrimary + " min-h-[44px] w-full sm:order-1 sm:min-h-0 sm:w-auto"}
            >
              View Transactions
            </button>
          </div>
        </div>
      )}
    </div>
    </AppShell>
  );
}
