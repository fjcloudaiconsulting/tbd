"use client";

import { useEffect, useRef, useState } from "react";

import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import {
  btnPrimary,
  btnSecondary,
  card,
  error as errorCls,
  input,
  label as labelCls,
} from "@/lib/styles";
import type {
  Account,
  ConvertToTransferRequest,
  Transaction,
  TransferCandidate,
  TransferCandidatesResponse,
} from "@/lib/types";

interface Props {
  source: Transaction;
  accounts: Account[];
  onConverted: () => void;
  onCancel: () => void;
}

export default function MarkAsTransferModal({
  source,
  accounts,
  onConverted,
  onCancel,
}: Props) {
  // Stage 1
  const [destAcctId, setDestAcctId] = useState<number | null>(null);

  // Stage 2
  const [candidates, setCandidates] = useState<TransferCandidate[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [selectedCandidateId, setSelectedCandidateId] = useState<number | null>(null);
  const [createInstead, setCreateInstead] = useState(false);

  // Submit
  const [recategorize, setRecategorize] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  // Source account currency (used to filter destination accounts list)
  const sourceAcct = accounts.find((a) => a.id === source.account_id);
  const sourceCurrency = sourceAcct?.currency ?? "EUR";

  const eligibleAccounts = accounts.filter(
    (a) => a.id !== source.account_id && a.currency === sourceCurrency
  );

  // Focus + restore
  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    cancelRef.current?.focus();
    return () => {
      previousFocusRef.current?.focus();
    };
  }, []);

  // Escape + focus trap
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) {
        e.stopPropagation();
        onCancel();
        return;
      }
      if (e.key === "Tab") {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusable || focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [submitting, onCancel]);

  // Fetch candidates when destination changes
  useEffect(() => {
    if (destAcctId === null) {
      setCandidates([]);
      setSelectedCandidateId(null);
      setCreateInstead(false);
      return;
    }
    let cancelled = false;
    setCandidatesLoading(true);
    setSelectedCandidateId(null);
    setCreateInstead(false);
    setErrorText(null);
    apiFetch<TransferCandidatesResponse>(
      `/api/v1/transactions/${source.id}/transfer-candidates?destination_account_id=${destAcctId}`,
      {}
    )
      .then((r) => {
        if (cancelled) return;
        setCandidates(r.candidates);
        // Pre-select single same-day candidate (opt-out)
        if (r.candidates.length === 1 && r.candidates[0].confidence === "same_day") {
          setSelectedCandidateId(r.candidates[0].id);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setErrorText(extractErrorMessage(e, "Failed to fetch candidates"));
      })
      .finally(() => {
        if (!cancelled) setCandidatesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [destAcctId, source.id]);

  const hasZeroCandidates =
    destAcctId !== null && !candidatesLoading && candidates.length === 0;
  const hasOneSameDay =
    candidates.length === 1 && candidates[0].confidence === "same_day";
  const hasOneNearDate =
    candidates.length === 1 && candidates[0].confidence === "near_date";
  const hasMulti = candidates.length >= 2;

  const canPair = !createInstead && selectedCandidateId !== null;
  const canCreate = createInstead || hasZeroCandidates;
  const canSubmit = (canPair || canCreate) && !submitting && destAcctId !== null;

  async function handleSubmit() {
    if (destAcctId === null) return;
    setSubmitting(true);
    setErrorText(null);
    try {
      const body: ConvertToTransferRequest = {
        destination_account_id: destAcctId,
        recategorize,
      };
      if (canPair && selectedCandidateId !== null) {
        body.pair_with_transaction_id = selectedCandidateId;
      }
      await apiFetch(`/api/v1/transactions/${source.id}/convert-to-transfer`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      onConverted();
    } catch (err) {
      setErrorText(extractErrorMessage(err, "Failed to convert to transfer"));
    } finally {
      setSubmitting(false);
    }
  }

  const destAcctName =
    destAcctId !== null
      ? accounts.find((a) => a.id === destAcctId)?.name ?? ""
      : "";

  const primaryLabel = canPair ? "Pair as transfer" : "Create partner leg";

  function renderCandidateRow(c: TransferCandidate) {
    const checked = selectedCandidateId === c.id;
    const diffText =
      c.date_diff_days === 0
        ? "same day"
        : `${c.date_diff_days} day${c.date_diff_days === 1 ? "" : "s"} off`;
    const ariaLabel = `${c.account_name} ${c.date} ${formatAmount(c.amount)} ${c.description}`;
    return (
      <label
        key={c.id}
        className="flex cursor-pointer items-start gap-2 rounded border border-border p-2 text-sm text-text-primary"
      >
        <input
          type="radio"
          name="transfer-candidate"
          value={c.id}
          checked={checked}
          onChange={() => {
            setSelectedCandidateId(c.id);
            setCreateInstead(false);
          }}
          aria-label={ariaLabel}
          className="mt-1"
        />
        <span className="flex flex-col">
          <span>
            {c.date} &middot; {c.description} &middot; {formatAmount(c.amount)}
          </span>
          <span className="text-xs text-text-secondary">
            {c.account_name} &middot; {diffText}
          </span>
        </span>
      </label>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="mark-transfer-title"
        className={`${card} w-full max-w-md p-6 shadow-xl`}
      >
        <h2
          id="mark-transfer-title"
          className="mb-4 text-lg font-semibold text-text-primary"
        >
          Mark as transfer
        </h2>

        <div className="mb-4 text-sm text-text-primary">
          <div>
            <span className="font-medium">Source:</span> {source.account_name} &middot;{" "}
            {source.type === "expense" ? "-" : "+"}
            {formatAmount(source.amount)} &middot; {source.date}
          </div>
        </div>

        {/* Stage 1 */}
        <div className="mb-4">
          <label className={labelCls}>Destination account</label>
          <select
            className={input}
            value={destAcctId === null ? "" : destAcctId}
            onChange={(e) => {
              const v = e.target.value;
              setDestAcctId(v === "" ? null : Number(v));
            }}
          >
            <option value="">Select account...</option>
            {eligibleAccounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </div>

        {/* Stage 2 */}
        {destAcctId !== null && candidatesLoading && (
          <div className="mb-4 text-sm text-text-secondary">Loading candidates...</div>
        )}

        {hasZeroCandidates && (
          <div className="mb-4 rounded border border-border p-3 text-sm text-text-primary">
            No matching un-linked rows found in {destAcctName}. Create the partner leg
            now?
          </div>
        )}

        {hasOneSameDay && (
          <div className="mb-4 space-y-2">
            {renderCandidateRow(candidates[0])}
            <button
              type="button"
              onClick={() => {
                setCreateInstead(true);
                setSelectedCandidateId(null);
              }}
              className={btnSecondary}
            >
              Or create a new partner leg instead
            </button>
          </div>
        )}

        {hasOneNearDate && (
          <div className="mb-4 space-y-2">
            {renderCandidateRow(candidates[0])}
            <p className="text-xs text-text-secondary">
              Date differs by {candidates[0].date_diff_days} day
              {candidates[0].date_diff_days === 1 ? "" : "s"}.
            </p>
            <button
              type="button"
              onClick={() => {
                setCreateInstead(true);
                setSelectedCandidateId(null);
              }}
              className={btnSecondary}
            >
              Or create a new partner leg instead
            </button>
          </div>
        )}

        {hasMulti && (
          <div className="mb-4 space-y-2">
            {candidates.map(renderCandidateRow)}
            <button
              type="button"
              onClick={() => {
                setCreateInstead(true);
                setSelectedCandidateId(null);
              }}
              className={btnSecondary}
            >
              Create a new partner leg instead
            </button>
          </div>
        )}

        {createInstead && (
          <p className="mb-4 text-sm text-text-secondary">
            A new partner leg will be created in {destAcctName}.
          </p>
        )}

        {/* Recategorize */}
        <label className="mb-4 flex items-center gap-2 text-sm text-text-primary">
          <input
            type="checkbox"
            checked={recategorize}
            onChange={(e) => setRecategorize(e.target.checked)}
            className="rounded"
          />
          Use system Transfer category for both legs
        </label>

        {errorText && (
          <div role="alert" className={`${errorCls} mb-4`}>
            {errorText}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className={btnSecondary}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className={btnPrimary}
          >
            {submitting ? "Saving..." : primaryLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
