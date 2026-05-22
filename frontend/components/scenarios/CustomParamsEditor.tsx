"use client";

/**
 * Custom plan params editor (PR3 of the Plans train).
 *
 * Architect-locked invariants:
 * - Five event types: income_off, expense_off, recurring_on,
 *   one_off_income, one_off_expense.
 * - All months are RELATIVE to the scenario start (month 0 = the
 *   month the simulate call is made in).
 * - one_off_income / one_off_expense amounts are Decimal strings
 *   (existing currency convention).
 * - Debounced re-simulate on every change. The DEBOUNCE happens at
 *   the parent (PlansPage's PlanEditor); this component is
 *   presentational and just emits the next params object via
 *   setParams.
 * - Adding an event opens a type picker; selecting the type
 *   prepends a default event card to the list and scrolls it into
 *   view.
 */

import {
  ChangeEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { input, label as labelCls, btnSecondary } from "@/lib/styles";

export type EventType =
  | "income_off"
  | "expense_off"
  | "recurring_on"
  | "one_off_income"
  | "one_off_expense";

const TYPE_LABEL: Record<EventType, string> = {
  income_off: "Income off",
  expense_off: "Expense off",
  recurring_on: "Recurring on",
  one_off_income: "One-off income",
  one_off_expense: "One-off expense",
};

interface Account {
  id: number;
  name: string;
  currency: string;
}

interface Category {
  id: number;
  name: string;
}

interface Recurring {
  id: number;
  description?: string | null;
  amount?: string;
}

interface CustomEvent {
  type: EventType;
  from_month?: number;
  to_month?: number | null;
  month?: number;
  amount?: string;
  account_id?: number;
  category_id?: number | null;
  category_ids?: number[];
  recurring_id?: number;
}

function defaultEvent(type: EventType, accounts: Account[]): CustomEvent {
  switch (type) {
    case "income_off":
      return { type: "income_off", from_month: 0, to_month: null };
    case "expense_off":
      return { type: "expense_off", from_month: 0, to_month: null };
    case "recurring_on":
      return { type: "recurring_on", recurring_id: 0, from_month: 0, to_month: null };
    case "one_off_income":
      return {
        type: "one_off_income",
        month: 0,
        amount: "0.00",
        account_id: accounts[0]?.id ?? 0,
      };
    case "one_off_expense":
      return {
        type: "one_off_expense",
        month: 0,
        amount: "0.00",
        account_id: accounts[0]?.id ?? 0,
      };
  }
}

export function CustomParamsEditor({
  params,
  setParams,
  accounts,
  categories = [],
  recurring = [],
}: {
  params: Record<string, unknown>;
  setParams: (next: Record<string, unknown>) => void;
  accounts: Account[];
  categories?: Category[];
  recurring?: Recurring[];
}) {
  const baseId = useId();
  const events = useMemo<CustomEvent[]>(() => {
    const raw = params.events;
    return Array.isArray(raw) ? (raw as CustomEvent[]) : [];
  }, [params]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const newestRef = useRef<HTMLLIElement | null>(null);

  const updateEvent = useCallback(
    (idx: number, patch: Partial<CustomEvent>) => {
      const next = events.map((ev, i) =>
        i === idx ? { ...ev, ...patch } : ev,
      );
      setParams({ ...params, events: next });
    },
    [events, params, setParams],
  );

  const removeEvent = useCallback(
    (idx: number) => {
      const next = events.filter((_, i) => i !== idx);
      setParams({ ...params, events: next });
    },
    [events, params, setParams],
  );

  const addEvent = useCallback(
    (type: EventType) => {
      const next = [defaultEvent(type, accounts), ...events];
      setParams({ ...params, events: next });
      setPickerOpen(false);
    },
    [accounts, events, params, setParams],
  );

  useEffect(() => {
    // Scroll the newest event card into view when one was just added.
    // Guarded so jsdom (which doesn't implement scrollIntoView) doesn't
    // throw in unit tests.
    if (
      newestRef.current
      && typeof newestRef.current.scrollIntoView === "function"
    ) {
      newestRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [events.length]);

  return (
    <div data-testid="custom-params-editor">
      <div className="mb-3">
        <label className={labelCls} htmlFor={`${baseId}-custom-label`}>
          Label
        </label>
        <input
          id={`${baseId}-custom-label`}
          value={(params.label as string) ?? ""}
          onChange={(e) =>
            setParams({ ...params, label: e.target.value })
          }
          className={input}
          data-testid="custom-label-input"
        />
      </div>

      <div className="mb-2 flex items-center justify-between">
        <p className={labelCls}>Events</p>
        <div className="relative">
          <button
            type="button"
            className={`${btnSecondary} sm:min-h-0`}
            onClick={() => setPickerOpen((open) => !open)}
            data-testid="custom-add-event"
          >
            + Add event
          </button>
          {pickerOpen && (
            <ul
              className="absolute right-0 z-10 mt-1 w-48 rounded-md border border-border bg-surface shadow-lg"
              data-testid="custom-event-picker"
            >
              {(Object.keys(TYPE_LABEL) as EventType[]).map((type) => (
                <li key={type}>
                  <button
                    type="button"
                    className="block w-full px-3 py-2 text-left text-sm hover:bg-bg"
                    onClick={() => addEvent(type)}
                    data-testid={`custom-event-picker-${type}`}
                  >
                    {TYPE_LABEL[type]}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {events.length === 0 ? (
        <p
          className="text-xs text-text-muted"
          data-testid="custom-events-empty"
        >
          No events yet. Add one to model a sabbatical, big purchase, or
          one-off cashflow.
        </p>
      ) : (
        <ul className="space-y-3">
          {events.map((ev, idx) => (
            <li
              key={idx}
              ref={idx === 0 ? newestRef : null}
              className="rounded-md border border-border p-3"
              data-testid={`custom-event-card-${idx}`}
            >
              <div className="mb-2 flex items-center justify-between">
                <span
                  className="text-xs font-medium uppercase tracking-wide text-text-muted"
                  data-testid={`custom-event-type-${idx}`}
                >
                  {TYPE_LABEL[ev.type]}
                </span>
                <button
                  type="button"
                  className="text-xs text-danger underline-offset-2 hover:underline"
                  onClick={() => removeEvent(idx)}
                  data-testid={`custom-event-remove-${idx}`}
                >
                  Remove
                </button>
              </div>
              <EventFields
                event={ev}
                idx={idx}
                onChange={(patch) => updateEvent(idx, patch)}
                accounts={accounts}
                categories={categories}
                recurring={recurring}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EventFields({
  event,
  idx,
  onChange,
  accounts,
  categories,
  recurring,
}: {
  event: CustomEvent;
  idx: number;
  onChange: (patch: Partial<CustomEvent>) => void;
  accounts: Account[];
  categories: Category[];
  recurring: Recurring[];
}) {
  function numHandler(field: keyof CustomEvent) {
    return (e: ChangeEvent<HTMLInputElement>) => {
      const value = e.target.value;
      onChange({ [field]: value === "" ? null : Number(value) } as Partial<CustomEvent>);
    };
  }

  const monthRangeFields = (
    <div className="grid grid-cols-2 gap-2">
      <div>
        <label className={labelCls} htmlFor={`from-${idx}`}>
          From month
        </label>
        <input
          id={`from-${idx}`}
          type="number"
          min={0}
          value={event.from_month ?? 0}
          onChange={numHandler("from_month")}
          className={input}
          data-testid={`custom-event-from-${idx}`}
        />
      </div>
      <div>
        <label className={labelCls} htmlFor={`to-${idx}`}>
          To month (optional)
        </label>
        <input
          id={`to-${idx}`}
          type="number"
          min={0}
          value={event.to_month ?? ""}
          onChange={numHandler("to_month")}
          className={input}
          data-testid={`custom-event-to-${idx}`}
        />
      </div>
    </div>
  );

  if (event.type === "income_off") return monthRangeFields;

  if (event.type === "expense_off") {
    return (
      <div className="space-y-2">
        {monthRangeFields}
        {categories.length > 0 && (
          <div>
            <label className={labelCls} htmlFor={`catids-${idx}`}>
              Limit to categories (optional, comma-sep ids)
            </label>
            <input
              id={`catids-${idx}`}
              type="text"
              value={(event.category_ids ?? []).join(",")}
              onChange={(e) =>
                onChange({
                  category_ids: e.target.value
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean)
                    .map(Number)
                    .filter((n) => !Number.isNaN(n)),
                })
              }
              className={input}
              data-testid={`custom-event-categories-${idx}`}
            />
          </div>
        )}
      </div>
    );
  }

  if (event.type === "recurring_on") {
    return (
      <div className="space-y-2">
        <div>
          <label className={labelCls} htmlFor={`rec-${idx}`}>
            Recurring template
          </label>
          <select
            id={`rec-${idx}`}
            value={event.recurring_id ?? 0}
            onChange={(e) => onChange({ recurring_id: Number(e.target.value) })}
            className={input}
            data-testid={`custom-event-recurring-${idx}`}
          >
            <option value={0} disabled>
              Pick a recurring template
            </option>
            {recurring.map((r) => (
              <option key={r.id} value={r.id}>
                {r.description ?? `Recurring ${r.id}`}
              </option>
            ))}
          </select>
        </div>
        {monthRangeFields}
      </div>
    );
  }

  if (
    event.type === "one_off_income" ||
    event.type === "one_off_expense"
  ) {
    return (
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className={labelCls} htmlFor={`month-${idx}`}>
            Month
          </label>
          <input
            id={`month-${idx}`}
            type="number"
            min={0}
            value={event.month ?? 0}
            onChange={(e) => onChange({ month: Number(e.target.value) })}
            className={input}
            data-testid={`custom-event-month-${idx}`}
          />
        </div>
        <div>
          <label className={labelCls} htmlFor={`amount-${idx}`}>
            Amount
          </label>
          <input
            id={`amount-${idx}`}
            type="number"
            step="0.01"
            min="0"
            value={event.amount ?? "0.00"}
            onChange={(e) => onChange({ amount: e.target.value })}
            className={input}
            data-testid={`custom-event-amount-${idx}`}
          />
        </div>
        <div className="col-span-2">
          <label className={labelCls} htmlFor={`account-${idx}`}>
            Account
          </label>
          <select
            id={`account-${idx}`}
            value={event.account_id ?? 0}
            onChange={(e) => onChange({ account_id: Number(e.target.value) })}
            className={input}
            data-testid={`custom-event-account-${idx}`}
          >
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} ({a.currency})
              </option>
            ))}
          </select>
        </div>
      </div>
    );
  }

  return null;
}
