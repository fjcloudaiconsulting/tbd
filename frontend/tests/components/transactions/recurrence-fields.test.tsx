/**
 * TBD-762. `RecurrenceFields`, extracted from the quick-entry form's inline
 * Repeats block and reused by the recurring page's Edit series dialog.
 *
 *  R1  "inline" keeps the quick-entry form's ids, hidden labels, placeholder,
 *      hint wiring and the five frequencies in order (the form's own suite,
 *      `floating/transaction-form.test.tsx`, still drives it end to end).
 *  R2  Every control reports through its callback.
 *  R3  "stacked" makes the end explicit: Never disables the count, After
 *      enables it, and progress and errors are wired to the count input.
 */
import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import RecurrenceFields, { FREQUENCY_OPTIONS } from "@/components/transactions/RecurrenceFields";

function noop() {}

describe("RecurrenceFields (TBD-762)", () => {
  it("R1: inline keeps the quick-entry form's markup contract", () => {
    render(
      <RecurrenceFields
        idPrefix="fab-tx"
        frequency="monthly"
        onFrequencyChange={noop}
        occurrenceCount=""
        onOccurrenceCountChange={noop}
        autoSettle={false}
        onAutoSettleChange={noop}
      />,
    );
    const freq = screen.getByLabelText("Frequency") as HTMLSelectElement;
    expect(freq.id).toBe("fab-tx-freq");
    expect(freq.value).toBe("monthly");
    expect([...freq.options].map((o) => [o.value, o.text])).toEqual([
      ["weekly", "Weekly"],
      ["biweekly", "Every 2 weeks"],
      ["monthly", "Monthly"],
      ["quarterly", "Quarterly"],
      ["yearly", "Yearly"],
    ]);
    expect(FREQUENCY_OPTIONS.map((o) => o.value)).toEqual(
      ["weekly", "biweekly", "monthly", "quarterly", "yearly"],
    );

    const count = screen.getByLabelText("Number of payments") as HTMLInputElement;
    expect(count.id).toBe("fab-tx-occurrences");
    expect(count.type).toBe("text");
    expect(count.placeholder).toBe("Payments (optional)");
    expect(count.getAttribute("aria-describedby")).toBe("fab-tx-occurrences-hint");
    expect(document.getElementById("fab-tx-occurrences-hint")?.textContent).toContain(
      "Leave blank to repeat indefinitely.",
    );
    expect(screen.getByLabelText("Auto-settle")).not.toBeChecked();
    // No Ends choice in the inline variant: blank is "indefinitely".
    expect(screen.queryByRole("radio")).toBeNull();
  });

  it("R2: each control reports through its callback", () => {
    const onFrequencyChange = vi.fn();
    const onOccurrenceCountChange = vi.fn();
    const onAutoSettleChange = vi.fn();
    render(
      <RecurrenceFields
        idPrefix="fab-tx"
        frequency="monthly"
        onFrequencyChange={onFrequencyChange}
        occurrenceCount=""
        onOccurrenceCountChange={onOccurrenceCountChange}
        autoSettle={false}
        onAutoSettleChange={onAutoSettleChange}
      />,
    );
    fireEvent.change(screen.getByLabelText("Frequency"), { target: { value: "yearly" } });
    fireEvent.change(screen.getByLabelText("Number of payments"), { target: { value: "4" } });
    fireEvent.click(screen.getByLabelText("Auto-settle"));
    expect(onFrequencyChange).toHaveBeenCalledWith("yearly");
    expect(onOccurrenceCountChange).toHaveBeenCalledWith("4");
    expect(onAutoSettleChange).toHaveBeenCalledWith(true);
  });

  function Stacked({ countError = null }: { countError?: string | null }) {
    const [never, setNever] = useState(true);
    const [count, setCount] = useState("");
    return (
      <RecurrenceFields
        variant="stacked"
        idPrefix="edit-series"
        frequency="weekly"
        onFrequencyChange={noop}
        occurrenceCount={count}
        onOccurrenceCountChange={setCount}
        autoSettle
        onAutoSettleChange={noop}
        endsNever={never}
        onEndsNeverChange={setNever}
        progress="3 of 12 done."
        countError={countError}
      />
    );
  }

  it("R3: stacked shows visible labels and an explicit Never / After choice", () => {
    render(<Stacked />);
    expect((screen.getByLabelText("Frequency") as HTMLSelectElement).value).toBe("weekly");
    expect(screen.getByText("Frequency").className).not.toContain("sr-only");
    expect(screen.getByRole("group", { name: "Ends" })).toBeInTheDocument();
    expect(screen.getByLabelText("Auto-settle")).toBeChecked();

    const count = screen.getByLabelText("Number of occurrences");
    expect(screen.getByLabelText("Never")).toBeChecked();
    expect(count).toBeDisabled();

    fireEvent.click(screen.getByLabelText("After"));
    expect(screen.getByLabelText("Never")).not.toBeChecked();
    expect(count).not.toBeDisabled();
    fireEvent.change(count, { target: { value: "8" } });
    expect((count as HTMLInputElement).value).toBe("8");
    expect(count.getAttribute("aria-describedby")).toBe("edit-series-occurrences-progress");
    expect(screen.getByText("3 of 12 done.").id).toBe("edit-series-occurrences-progress");
  });

  it("R3: a count error is shown and wired to the input", () => {
    render(<Stacked countError="Enter a whole number from 1 to 1200." />);
    const count = screen.getByLabelText("Number of occurrences");
    expect(count).toHaveAttribute("aria-invalid", "true");
    expect(count.getAttribute("aria-describedby")).toBe(
      "edit-series-occurrences-progress edit-series-occurrences-error",
    );
    expect(screen.getByText("Enter a whole number from 1 to 1200.")).toBeInTheDocument();
  });
});
