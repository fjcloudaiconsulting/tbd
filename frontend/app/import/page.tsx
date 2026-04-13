"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import type {
  Account,
  Category,
  ImportConfirmResponse,
  ImportConfirmRow,
  ImportPreviewResponse,
  ImportPreviewRow,
} from "@/lib/types";

const btnSecondary = "rounded-md border border-border px-4 py-2 text-sm font-medium text-text-secondary hover:bg-surface-raised transition-colors";

type Step = "upload" | "preview" | "results";

export default function ImportPage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // ── Shared data ──────────────────────────────────────────────────────────
  const { data: accounts } = useSWR<Account[]>("accounts", () => apiFetch<Account[]>("/api/v1/accounts"));
  const { data: categories } = useSWR<Category[]>("categories", () => apiFetch<Category[]>("/api/v1/categories"));

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
          category_id: null,
          skip: r.is_duplicate, // pre-skip duplicates
          is_transfer: false,
          transfer_account_id: null,
        })),
      );
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

    try {
      const data = await apiFetch<ImportConfirmResponse>("/api/v1/import/confirm", {
        method: "POST",
        body: JSON.stringify({
          account_id: preview.account_id,
          default_category_id: defaultCategoryId,
          rows: rowStates,
        }),
      });
      setResults(data);
      setStep("results");
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Import failed"));
    } finally {
      setLoading(false);
    }
  }, [preview, defaultCategoryId, rowStates]);

  // ── Row update helpers ───────────────────────────────────────────────────
  const updateRow = useCallback((rowNum: number, patch: Partial<ImportConfirmRow>) => {
    setRowStates((prev) =>
      prev.map((r) => (r.row_number === rowNum ? { ...r, ...patch } : r)),
    );
  }, []);

  const activeRows = rowStates.filter((r) => !r.skip);
  const skipCount = rowStates.filter((r) => r.skip).length;

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className={pageTitle}>Import Transactions</h1>
        <button onClick={() => router.back()} className={btnSecondary}>
          Back
        </button>
      </div>

      {errorMsg && <div className={errorCls}>{errorMsg}</div>}

      {/* ── Step 1: Upload ──────────────────────────────────────────────── */}
      {step === "upload" && (
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
              className={btnPrimary}
            >
              {loading ? "Parsing..." : "Upload & Preview"}
            </button>
          </div>
        </div>
      )}

      {/* ── Step 2: Preview ─────────────────────────────────────────────── */}
      {step === "preview" && preview && (
        <div className="space-y-4">
          {/* Summary bar */}
          <div className={card}>
            <div className="flex flex-wrap items-center gap-4 px-6 py-4 text-sm">
              <span className="font-medium text-text-primary">{preview.file_name}</span>
              <span className="text-text-muted">{preview.total_rows} transactions</span>
              {preview.duplicate_count > 0 && (
                <span className="rounded bg-warning-dim px-2 py-0.5 text-warning">
                  {preview.duplicate_count} duplicates
                </span>
              )}
              {preview.transfer_candidate_count > 0 && (
                <span className="rounded bg-accent/10 px-2 py-0.5 text-accent">
                  {preview.transfer_candidate_count} potential transfers
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

          {/* Preview table */}
          <div className={card + " overflow-x-auto"}>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left">
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Skip</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Date</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Description</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Amount</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Type</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Category</th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-text-muted">Transfer</th>
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((previewRow, idx) => {
                  const rowState = rowStates[idx];
                  if (!rowState) return null;
                  const catOptions = rowState.type === "income" ? incomeCategories : expenseCategories;
                  const isDup = previewRow.is_duplicate;
                  const isTransfer = previewRow.is_potential_transfer;

                  let rowBg = "";
                  if (rowState.skip) rowBg = "opacity-40";
                  else if (isDup) rowBg = "bg-warning-dim";
                  else if (isTransfer || rowState.is_transfer) rowBg = "bg-accent/5";

                  return (
                    <tr key={previewRow.row_number} className={`border-b border-border ${rowBg}`}>
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
                        {!rowState.skip && !rowState.is_transfer && (
                          <select
                            value={rowState.category_id ?? ""}
                            onChange={(e) =>
                              updateRow(previewRow.row_number, {
                                category_id: e.target.value === "" ? null : Number(e.target.value),
                              })
                            }
                            className={input + " !w-40"}
                          >
                            <option value="">Default</option>
                            {catOptions.map((c) => (
                              <option key={c.id} value={c.id}>{c.name}</option>
                            ))}
                          </select>
                        )}
                      </td>
                      <td className="px-4 py-2">
                        {!rowState.skip && (
                          <div className="flex items-center gap-2">
                            <input
                              type="checkbox"
                              checked={rowState.is_transfer}
                              onChange={(e) =>
                                updateRow(previewRow.row_number, {
                                  is_transfer: e.target.checked,
                                  transfer_account_id: null,
                                })
                              }
                              className="rounded border-border"
                            />
                            {rowState.is_transfer && (
                              <select
                                value={rowState.transfer_account_id ?? ""}
                                onChange={(e) =>
                                  updateRow(previewRow.row_number, {
                                    transfer_account_id: e.target.value === "" ? null : Number(e.target.value),
                                  })
                                }
                                className={input + " !w-40"}
                              >
                                <option value="">Select account...</option>
                                {activeAccounts
                                  .filter((a) => a.id !== preview.account_id)
                                  .map((a) => (
                                    <option key={a.id} value={a.id}>{a.name}</option>
                                  ))}
                              </select>
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-4">
            <button
              onClick={handleConfirm}
              disabled={defaultCategoryId === "" || activeRows.length === 0 || loading}
              className={btnPrimary}
            >
              {loading
                ? "Importing..."
                : `Import ${activeRows.length} transaction${activeRows.length === 1 ? "" : "s"}`}
            </button>
            <button
              onClick={() => { setStep("upload"); setPreview(null); setFile(null); }}
              className={btnSecondary}
            >
              Start Over
            </button>
            {skipCount > 0 && (
              <span className="text-sm text-text-muted">{skipCount} skipped</span>
            )}
          </div>
        </div>
      )}

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
          <div className="flex gap-4 border-t border-border px-6 py-4">
            <button onClick={() => router.push("/transactions")} className={btnPrimary}>
              View Transactions
            </button>
            <button
              onClick={() => { setStep("upload"); setPreview(null); setResults(null); setFile(null); }}
              className={btnSecondary}
            >
              Import Another File
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
