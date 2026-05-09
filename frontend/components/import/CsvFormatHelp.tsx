"use client";

import { useEffect, useRef, useState } from "react";
import { btnLink, btnSecondary, card } from "@/lib/styles";

/**
 * Pre-upload disclosure that surfaces what the importer actually accepts,
 * so users do not discover format mismatches mid-flow.
 *
 * Source of truth for the copy below is backend/app/services/import_parser.py:
 *   - Auto-detects ; or , as the delimiter (semicolon checked first because
 *     ING NL exports use it and may carry commas inside quoted fields).
 *   - Required header columns: Date, Name / Description, Debit/credit,
 *     Amount (EUR). Optional: Counterparty, Transaction type, Account,
 *     Code, Notifications, Resulting balance, Tag.
 *   - Date format: 8-digit YYYYMMDD (e.g. 20260406).
 *   - Amount format: European, comma decimal separator, optional period
 *     thousands separator. Sign is encoded by the Debit/credit column,
 *     not by a leading minus.
 *   - Encoding: UTF-8. A leading BOM is tolerated.
 *
 * Other locales (US "." decimal, ISO dates, different headers) are not
 * supported yet (see project_localized_import_intelligence.md).
 */

const SAMPLE_CSV =
  '"Date";"Name / Description";"Account";"Counterparty";"Code";"Debit/credit";"Amount (EUR)";"Transaction type";"Notifications"\n' +
  '"20260406";"Albert Heijn 1234";"NL00BANK0000000000";"NL00ALBE0000000000";"BA";"Debit";"42,17";"Payment terminal";"Pasvolgnr: 001"\n' +
  '"20260405";"Salary Acme BV";"NL00BANK0000000000";"NL00ACME0000000000";"OV";"Credit";"3.250,00";"Online Banking";"Salary March"\n' +
  '"20260404";"Spotify";"NL00BANK0000000000";"NL00SPOT0000000000";"ID";"Debit";"10,99";"iDEAL";"Subscription"\n';

function downloadSample() {
  const blob = new Blob([SAMPLE_CSV], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "pfv-import-example.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function CsvFormatHelp() {
  const [showExample, setShowExample] = useState(false);

  return (
    <>
      <details className={`${card} group`}>
        <summary
          className="flex cursor-pointer list-none items-center justify-between gap-3 px-6 py-4 text-sm font-medium text-text-primary"
          aria-label="Toggle expected file format help"
        >
          <span className="flex items-center gap-2">
            <span aria-hidden className="text-text-muted">[?]</span>
            Expected file format
          </span>
          <span className="text-xs uppercase tracking-wider text-text-muted group-open:hidden">
            Show
          </span>
          <span className="hidden text-xs uppercase tracking-wider text-text-muted group-open:inline">
            Hide
          </span>
        </summary>

        <div className="space-y-3 border-t border-border px-6 py-4 text-sm text-text-secondary">
          <p>
            The importer currently accepts ING-style CSV exports.
            Files are parsed in memory and previewed before any
            transaction is written.
          </p>

          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-[max-content_1fr]">
            <dt className="font-semibold text-text-primary">Delimiters</dt>
            <dd>
              Semicolon (<code>;</code>) or comma (<code>,</code>). Auto-detected
              from the header line.
            </dd>

            <dt className="font-semibold text-text-primary">Required headers</dt>
            <dd>
              <code>Date</code>, <code>Name / Description</code>,
              {" "}<code>Debit/credit</code>, <code>Amount (EUR)</code>.
            </dd>

            <dt className="font-semibold text-text-primary">Optional headers</dt>
            <dd>
              <code>Counterparty</code>, <code>Transaction type</code>,
              {" "}<code>Account</code>, <code>Code</code>,
              {" "}<code>Notifications</code>, <code>Resulting balance</code>,
              {" "}<code>Tag</code>.
            </dd>

            <dt className="font-semibold text-text-primary">Date format</dt>
            <dd>
              <code>YYYYMMDD</code> with no separators (for example
              {" "}<code>20260406</code> for 6 April 2026).
            </dd>

            <dt className="font-semibold text-text-primary">Decimal format</dt>
            <dd>
              European: comma is the decimal separator, period is an optional
              thousands separator (for example <code>1.234,56</code>). The sign
              comes from <code>Debit/credit</code>, so amounts stay positive.
            </dd>

            <dt className="font-semibold text-text-primary">Encoding</dt>
            <dd>UTF-8. A leading byte order mark (BOM) is accepted.</dd>
          </dl>

          <div className="flex flex-wrap items-center gap-3 pt-2">
            <button
              type="button"
              onClick={downloadSample}
              className={btnSecondary}
            >
              Download example CSV
            </button>
            <button
              type="button"
              onClick={() => setShowExample(true)}
              className={btnLink}
            >
              View example
            </button>
          </div>

          <p className="pt-1 text-xs text-text-muted">
            Other formats (US decimal point, ISO dates, locale-specific bank
            exports) are coming soon as part of the localized-import work.
          </p>
        </div>
      </details>

      {showExample && (
        <CsvExampleModal onClose={() => setShowExample(false)} />
      )}
    </>
  );
}

function CsvExampleModal({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    closeRef.current?.focus();
    return () => {
      previousFocusRef.current?.focus();
    };
  }, []);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="csv-example-title"
        className={`${card} w-full max-w-2xl p-6 shadow-xl`}
      >
        <div className="mb-4 flex items-start justify-between gap-3">
          <h2
            id="csv-example-title"
            className="text-lg font-semibold text-text-primary"
          >
            Example CSV (3 rows)
          </h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className={btnSecondary}
            aria-label="Close example"
          >
            Close
          </button>
        </div>

        <p className="mb-3 text-sm text-text-secondary">
          Semicolon-delimited, UTF-8, European decimals, YYYYMMDD dates.
          Quotes around values are optional, and the importer trims them.
        </p>

        <pre className="max-h-72 overflow-auto rounded-md border border-border bg-surface-raised p-3 text-xs text-text-primary">
          <code>{SAMPLE_CSV}</code>
        </pre>
      </div>
    </div>
  );
}
