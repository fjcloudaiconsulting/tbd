/**
 * Tests for CustomParamsEditor (PR3 of the Plans train).
 *
 * Architect-locked checks:
 * - Add event opens the type picker; picking a type prepends the
 *   default event card.
 * - Remove event removes the card from the list and from the
 *   emitted params.
 * - Changing an event's field surfaces in the next setParams call.
 * - The editor handles each of the 5 event types.
 */
import React, { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import { CustomParamsEditor } from "@/components/scenarios/CustomParamsEditor";

interface Wrapped {
  onParams?: (p: Record<string, unknown>) => void;
  initialParams?: Record<string, unknown>;
}

function Wrapper({ onParams, initialParams }: Wrapped) {
  const [params, setParams] = useState<Record<string, unknown>>(
    initialParams ?? { label: "Sabbatical", events: [] },
  );
  return (
    <CustomParamsEditor
      params={params}
      setParams={(next) => {
        setParams(next);
        if (onParams) onParams(next);
      }}
      accounts={[
        { id: 12, name: "Main", currency: "EUR" },
        { id: 13, name: "Savings", currency: "EUR" },
      ]}
      categories={[{ id: 1, name: "Groceries" }]}
      recurring={[{ id: 9, description: "Salary" }]}
    />
  );
}

describe("CustomParamsEditor", () => {
  it("renders the empty events state when params.events is empty", () => {
    render(<Wrapper />);
    expect(
      screen.getByTestId("custom-events-empty"),
    ).toBeInTheDocument();
  });

  it("opens the event picker and adds a one_off_income event", () => {
    const onParams = vi.fn();
    render(<Wrapper onParams={onParams} />);
    fireEvent.click(screen.getByTestId("custom-add-event"));
    expect(screen.getByTestId("custom-event-picker")).toBeInTheDocument();
    fireEvent.click(
      screen.getByTestId("custom-event-picker-one_off_income"),
    );
    // The event card renders with the picked type.
    expect(screen.getByTestId("custom-event-card-0")).toBeInTheDocument();
    expect(screen.getByTestId("custom-event-type-0")).toHaveTextContent(
      /one-off income/i,
    );
    expect(onParams).toHaveBeenCalled();
    const lastCall = onParams.mock.calls.at(-1)![0] as {
      events: Array<{ type: string }>;
    };
    expect(lastCall.events[0].type).toBe("one_off_income");
  });

  it("removes an event when the Remove button is clicked", () => {
    const initial = {
      label: "S",
      events: [
        {
          type: "one_off_income",
          month: 5,
          amount: "1000.00",
          account_id: 12,
        },
      ],
    };
    const onParams = vi.fn();
    render(<Wrapper initialParams={initial} onParams={onParams} />);
    expect(screen.getByTestId("custom-event-card-0")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("custom-event-remove-0"));
    expect(screen.queryByTestId("custom-event-card-0")).toBeNull();
    const lastCall = onParams.mock.calls.at(-1)![0] as {
      events: unknown[];
    };
    expect(lastCall.events).toEqual([]);
  });

  it("updates the amount field on a one_off_expense event", () => {
    const initial = {
      label: "S",
      events: [
        {
          type: "one_off_expense",
          month: 3,
          amount: "500.00",
          account_id: 12,
        },
      ],
    };
    const onParams = vi.fn();
    render(<Wrapper initialParams={initial} onParams={onParams} />);
    const amountInput = screen.getByTestId(
      "custom-event-amount-0",
    ) as HTMLInputElement;
    fireEvent.change(amountInput, { target: { value: "750.00" } });
    const lastCall = onParams.mock.calls.at(-1)![0] as {
      events: Array<{ amount: string }>;
    };
    expect(lastCall.events[0].amount).toBe("750.00");
  });

  it("renders the income_off form with from/to fields", () => {
    const initial = {
      label: "S",
      events: [
        { type: "income_off", from_month: 0, to_month: 5 },
      ],
    };
    render(<Wrapper initialParams={initial} />);
    expect(
      screen.getByTestId("custom-event-from-0"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("custom-event-to-0"),
    ).toBeInTheDocument();
  });

  it("renders the expense_off form with category_ids field", () => {
    const initial = {
      label: "S",
      events: [
        { type: "expense_off", from_month: 0, to_month: 5, category_ids: [1] },
      ],
    };
    render(<Wrapper initialParams={initial} />);
    expect(
      screen.getByTestId("custom-event-categories-0"),
    ).toHaveValue("1");
  });

  it("renders the recurring_on form with recurring select", () => {
    const initial = {
      label: "S",
      events: [
        { type: "recurring_on", recurring_id: 9, from_month: 0, to_month: 5 },
      ],
    };
    render(<Wrapper initialParams={initial} />);
    expect(
      screen.getByTestId("custom-event-recurring-0"),
    ).toBeInTheDocument();
  });

  it("changing the label updates params.label", () => {
    const onParams = vi.fn();
    render(<Wrapper onParams={onParams} />);
    fireEvent.change(screen.getByTestId("custom-label-input"), {
      target: { value: "Updated label" },
    });
    const lastCall = onParams.mock.calls.at(-1)![0] as { label: string };
    expect(lastCall.label).toBe("Updated label");
  });
});
