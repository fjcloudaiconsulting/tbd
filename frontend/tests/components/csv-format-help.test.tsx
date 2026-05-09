/**
 * Direct coverage for the pre-upload CsvFormatHelp disclosure.
 *
 * The component pulls double duty on the import page: a collapsed
 * <details> summary with the full required/optional header list and
 * format rules, plus an in-page "View example" modal and a
 * "Download example CSV" Blob action. Each of those surfaces is
 * exercised here so a regression in copy or wiring fails loudly.
 *
 * Source of truth for asserted strings is the component itself
 * (frontend/components/import/CsvFormatHelp.tsx). The required and
 * optional header sets must stay aligned with the parser at
 * backend/app/services/import_parser.py.
 */
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CsvFormatHelp from "@/components/import/CsvFormatHelp";

describe("CsvFormatHelp", () => {
  beforeEach(() => {
    // The download path uses URL.createObjectURL / revokeObjectURL, which
    // jsdom does not implement. Stub them so we can assert the call sites
    // without crashing.
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(() => "blob:mock-url"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(),
    });

    // The download path also calls anchor.click(), which jsdom tries to
    // resolve as a navigation. We don't care about the navigation in this
    // test, so no-op the prototype click to keep stderr clean. Restored
    // by vi.restoreAllMocks() in afterEach.
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the disclosure collapsed by default with the body hidden", () => {
    render(<CsvFormatHelp />);

    // Summary is always present.
    const summary = screen.getByText("Expected file format");
    expect(summary).toBeInTheDocument();

    // The wrapping <details> must NOT be open initially.
    const details = summary.closest("details");
    expect(details).not.toBeNull();
    expect(details!.hasAttribute("open")).toBe(false);

    // Show/Hide labels: only "Show" should be the visible state. Both
    // labels live in the DOM (CSS toggles them via group-open:hidden), so
    // we assert on the open attribute rather than visibility classes.
    expect(screen.getByText("Show")).toBeInTheDocument();
    expect(screen.getByText("Hide")).toBeInTheDocument();
  });

  it("reveals the full format details once the disclosure is opened", () => {
    render(<CsvFormatHelp />);

    const details = screen.getByText("Expected file format").closest("details")!;
    // jsdom doesn't auto-open <details> on summary click in every version,
    // so set the attribute directly to mimic an expanded panel. The body
    // is rendered regardless; what we're asserting is that the copy is in
    // the DOM and reachable.
    details.setAttribute("open", "");

    // Intro line.
    expect(
      screen.getByText(/importer currently accepts ING-style CSV exports/i),
    ).toBeInTheDocument();

    // Definition list labels.
    expect(screen.getByText("Delimiters")).toBeInTheDocument();
    expect(screen.getByText("Required headers")).toBeInTheDocument();
    expect(screen.getByText("Optional headers")).toBeInTheDocument();
    expect(screen.getByText("Date format")).toBeInTheDocument();
    expect(screen.getByText("Decimal format")).toBeInTheDocument();
    expect(screen.getByText("Encoding")).toBeInTheDocument();

    // Date format value (YYYYMMDD with no separators).
    expect(screen.getByText(/no separators/i)).toBeInTheDocument();
    expect(screen.getByText("20260406")).toBeInTheDocument();

    // Decimal rule mentions European comma decimal and the Debit/credit sign.
    expect(
      screen.getByText(/comma is the decimal separator/i),
    ).toBeInTheDocument();
    expect(screen.getByText("1.234,56")).toBeInTheDocument();

    // Encoding line mentions UTF-8 and BOM tolerance.
    expect(
      screen.getByText(/UTF-8\. A leading byte order mark/i),
    ).toBeInTheDocument();
  });

  it("lists every required parser header inside the disclosure body", () => {
    render(<CsvFormatHelp />);
    screen
      .getByText("Expected file format")
      .closest("details")!
      .setAttribute("open", "");

    // Required headers from import_parser.py: Date, Name / Description,
    // Debit/credit, Amount (EUR). Each appears as a <code> element inside
    // the Required headers <dd>.
    const requiredDt = screen.getByText("Required headers");
    const requiredDd = requiredDt.nextElementSibling as HTMLElement;
    expect(requiredDd).not.toBeNull();
    const requiredText = requiredDd.textContent ?? "";
    expect(requiredText).toContain("Date");
    expect(requiredText).toContain("Name / Description");
    expect(requiredText).toContain("Debit/credit");
    expect(requiredText).toContain("Amount (EUR)");
  });

  it("lists every optional parser header inside the disclosure body", () => {
    render(<CsvFormatHelp />);
    screen
      .getByText("Expected file format")
      .closest("details")!
      .setAttribute("open", "");

    const optionalDt = screen.getByText("Optional headers");
    const optionalDd = optionalDt.nextElementSibling as HTMLElement;
    expect(optionalDd).not.toBeNull();
    const optionalText = optionalDd.textContent ?? "";

    // Optional headers from import_parser.py.
    for (const header of [
      "Counterparty",
      "Transaction type",
      "Account",
      "Code",
      "Notifications",
      "Resulting balance",
      "Tag",
    ]) {
      expect(optionalText).toContain(header);
    }
  });

  it("downloads a sample CSV via a Blob URL when 'Download example CSV' is clicked", () => {
    const createSpy = vi.spyOn(URL, "createObjectURL");
    const revokeSpy = vi.spyOn(URL, "revokeObjectURL");

    render(<CsvFormatHelp />);

    const downloadBtn = screen.getByRole("button", {
      name: /download example csv/i,
    });
    fireEvent.click(downloadBtn);

    // The handler builds a Blob, hands it to createObjectURL, simulates a
    // click on a synthetic anchor, then revokes the URL. We assert both
    // sides of the lifecycle to catch leaks if a future refactor drops
    // revokeObjectURL.
    expect(createSpy).toHaveBeenCalledTimes(1);
    const blobArg = createSpy.mock.calls[0][0] as Blob;
    expect(blobArg).toBeInstanceOf(Blob);
    expect(blobArg.type).toContain("text/csv");

    expect(revokeSpy).toHaveBeenCalledWith("blob:mock-url");
  });

  it("opens the example modal on 'View example' and renders the 3-row sample", () => {
    render(<CsvFormatHelp />);

    // No modal until the user clicks View example.
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /view example/i }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(
      screen.getByRole("heading", { name: /example csv \(3 rows\)/i }),
    ).toBeInTheDocument();

    // The three sample rows render inside a <pre><code> block. We don't
    // care about whitespace, only that each merchant from SAMPLE_CSV is
    // present so the user sees a realistic preview.
    const code = dialog.querySelector("code");
    expect(code).not.toBeNull();
    const sampleText = code!.textContent ?? "";
    expect(sampleText).toContain("Albert Heijn 1234");
    expect(sampleText).toContain("Salary Acme BV");
    expect(sampleText).toContain("Spotify");

    // And the format hint paragraph is present.
    expect(
      screen.getByText(/semicolon-delimited, utf-8, european decimals/i),
    ).toBeInTheDocument();
  });

  it("closes the example modal when the Close button is clicked", () => {
    render(<CsvFormatHelp />);

    fireEvent.click(screen.getByRole("button", { name: /view example/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /close example/i }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes the example modal on Escape", () => {
    render(<CsvFormatHelp />);

    fireEvent.click(screen.getByRole("button", { name: /view example/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes the example modal when clicking the backdrop", () => {
    render(<CsvFormatHelp />);

    fireEvent.click(screen.getByRole("button", { name: /view example/i }));
    const dialog = screen.getByRole("dialog");
    // Backdrop is the dialog's parent (the fixed inset-0 wrapper). The
    // component only closes when the click target equals currentTarget,
    // so click the wrapper directly.
    const backdrop = dialog.parentElement as HTMLElement;
    expect(backdrop).not.toBeNull();
    fireEvent.click(backdrop);

    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
