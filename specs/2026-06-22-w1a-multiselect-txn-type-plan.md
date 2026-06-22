# W1a — Reports Multi-Select Transaction-Type Filter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let a report widget filter by any combination of transaction types (Income / Expense / Transfer) via checkboxes, replacing today's single-select radio (Any / Income / Expense / Transfer).

**Architecture:** Frontend-only. The backend already supports `txn_type IN [...]` (transactions source advertises `("eq","in")`; query service compiles `.in_()`; query schema validates `op="in"` needs a non-empty list). We change the FE type to an array, the control to checkboxes, the resolver to emit `op:"in"`, and the chip to join the list. A shared `asTxnTypeArray` coercion keeps old reports (which persist `txn_type` as a string) working everywhere.

**Tech Stack:** Next 16 / React 19 / TypeScript, Vitest + Testing Library.

## Global Constraints
- No-Off-Token: only `globals.css` theme tokens for color (no raw Tailwind palette). This change touches no new colors.
- Verify the **full** vitest suite before claiming done (per `reference_frontend_full_suite_verification`) — not just the touched files.
- `op:"in"` requires a **non-empty** array; empty selection must emit **no** `txn_type` filter (preserves today's "Any").
- `transfer` is transactions-only; the Transfer checkbox is hidden for non-transactions sources and stripped from persisted config there.
- Branch: `feat/reports-multiselect-txn-type` (already created; specs already committed on it).

---

### Task 1: Type → array + shared coercion helper

**Files:**
- Modify: `frontend/lib/reports/types.ts:148`
- Modify: `frontend/lib/reports/resolve.ts` (add exported helper near top of module)
- Test: `frontend/tests/lib/reports/resolve.test.ts` (create if absent; else append)

**Interfaces:**
- Produces: `type TxnType = "income" | "expense" | "transfer"` (export from `types.ts`); `WidgetFilters.txn_type?: TxnType[]`; `asTxnTypeArray(v: unknown): TxnType[] | undefined` (export from `resolve.ts`).

- [ ] **Step 1: Write the failing test** (in `resolve.test.ts`)

```ts
import { asTxnTypeArray } from "@/lib/reports/resolve";

describe("asTxnTypeArray", () => {
  it("coerces a legacy string value to a one-element array", () => {
    expect(asTxnTypeArray("income")).toEqual(["income"]);
  });
  it("passes a valid array through", () => {
    expect(asTxnTypeArray(["income", "expense"])).toEqual(["income", "expense"]);
  });
  it("drops unknown members and returns undefined when empty", () => {
    expect(asTxnTypeArray(["bogus"])).toBeUndefined();
    expect(asTxnTypeArray([])).toBeUndefined();
    expect(asTxnTypeArray(undefined)).toBeUndefined();
    expect(asTxnTypeArray(null)).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run test, verify it fails**

Run: `docker compose exec frontend npx vitest run tests/lib/reports/resolve.test.ts -t asTxnTypeArray`
Expected: FAIL — `asTxnTypeArray` not exported.

- [ ] **Step 3: Implement**

In `types.ts`, add `export type TxnType = "income" | "expense" | "transfer";` and change line 148 to:
```ts
  txn_type?: TxnType[];
```
In `resolve.ts`, import `TxnType` and add near the top (after imports):
```ts
/**
 * Coerce a persisted txn_type into a clean array. Old saved reports
 * stored it as a single string; new reports store an array. Filters out
 * unknown members and returns undefined when nothing valid remains, so
 * callers treat "no valid types" the same as "no filter".
 */
export function asTxnTypeArray(v: unknown): TxnType[] | undefined {
  if (v == null) return undefined;
  const arr = Array.isArray(v) ? v : [v];
  const out = arr.filter(
    (x): x is TxnType => x === "income" || x === "expense" || x === "transfer",
  );
  return out.length > 0 ? out : undefined;
}
```

- [ ] **Step 4: Run test, verify it passes** (same command) → PASS.

- [ ] **Step 5: tsc** — `docker compose exec frontend npx tsc --noEmit` → expect type errors at the call sites (resolve emission, describe, FilterEditor, draft). Those are fixed in Tasks 2–5; note them, don't fix yet. Do NOT commit until Task 2 (resolver) so the module stays coherent.

---

### Task 2: Resolver emits `op:"in"`

**Files:**
- Modify: `frontend/lib/reports/resolve.ts:155-157`
- Test: `frontend/tests/lib/reports/resolve.test.ts`

**Interfaces:**
- Consumes: `asTxnTypeArray` (Task 1).

- [ ] **Step 1: Write the failing test**

```ts
import { resolveFilters } from "@/lib/reports/resolve";

describe("resolveFilters txn_type", () => {
  it("emits op:in for a multi-select array", () => {
    const out = resolveFilters(undefined, { txn_type: ["income", "expense"] });
    expect(out).toContainEqual({ field: "txn_type", op: "in", value: ["income", "expense"] });
  });
  it("emits nothing for an empty/undefined selection", () => {
    expect(resolveFilters(undefined, { txn_type: [] })).not.toContainEqual(
      expect.objectContaining({ field: "txn_type" }),
    );
    expect(resolveFilters(undefined, {})).not.toContainEqual(
      expect.objectContaining({ field: "txn_type" }),
    );
  });
  it("coerces a legacy string txn_type to op:in", () => {
    const out = resolveFilters(undefined, { txn_type: "expense" as unknown as ["expense"] });
    expect(out).toContainEqual({ field: "txn_type", op: "in", value: ["expense"] });
  });
});
```

- [ ] **Step 2: Run test, verify it fails** — `... vitest run tests/lib/reports/resolve.test.ts -t "resolveFilters txn_type"` → FAIL (current code emits `op:"eq"`).

- [ ] **Step 3: Implement** — replace `resolve.ts:155-157`:
```ts
  const txnTypes = asTxnTypeArray(widget?.txn_type);
  if (txnTypes) {
    out.push({ field: "txn_type", op: "in", value: txnTypes });
  }
```

- [ ] **Step 4: Run test, verify it passes** → PASS.

- [ ] **Step 5: Commit**
```bash
git add frontend/lib/reports/types.ts frontend/lib/reports/resolve.ts frontend/tests/lib/reports/resolve.test.ts
git commit -m "feat(reports): multi-select txn_type — array type + asTxnTypeArray + resolver op:in"
```

---

### Task 3: Chip joins the list

**Files:**
- Modify: `frontend/lib/reports/describe-filters.ts:18` (import) and `:75-78`
- Test: `frontend/tests/components/reports/widget-filter-chips.test.tsx`

**Interfaces:**
- Consumes: `asTxnTypeArray` (Task 1).

- [ ] **Step 1: Write/extend the failing test** — assert a multi-select chip reads "Income, Expense". Inspect the existing test file first for its helper/render shape and add a case that builds a widget with `config.filters.txn_type: ["income","expense"]` and expects the chip label `"Income, Expense"`. Also a legacy-string case (`txn_type: "income"`) → `"Income"`.

- [ ] **Step 2: Run test, verify it fails** — `... vitest run tests/components/reports/widget-filter-chips.test.tsx` → FAIL.

- [ ] **Step 3: Implement** — add `asTxnTypeArray` to the `resolve` import on line 18, then replace `:76-78`:
```ts
  const txnTypes = asTxnTypeArray(widgetFilters.txn_type);
  if (txnTypes) {
    chips.push({ key: "txn_type", label: txnTypes.map(capitalize).join(", ") });
  }
```

- [ ] **Step 4: Run test, verify it passes** → PASS.

- [ ] **Step 5: Commit**
```bash
git add frontend/lib/reports/describe-filters.ts frontend/tests/components/reports/widget-filter-chips.test.tsx
git commit -m "feat(reports): txn_type filter chip joins multi-select list"
```

---

### Task 4: Checkbox control replaces the radio row

**Files:**
- Modify: `frontend/components/reports/config/FilterEditor.tsx:15` (import), `:116-122` (usage), `:177-228` (component)
- Test: `frontend/tests/app/reports-editor-page.test.tsx` (and any test asserting the old radio)

**Interfaces:**
- Consumes: `asTxnTypeArray`, `TxnType`.
- Produces: `TxnTypeCheckboxRow` replacing `TxnTypeRadioRow`. `onChange(next: TxnType[] | undefined)`.

- [ ] **Step 1: Write the failing test** — in the editor-page (or a focused FilterEditor) test: render the Filters tab on a transactions widget, assert checkboxes labelled `Widget transaction type Income/Expense/Transfer` exist (role `checkbox`), check Income + Expense, assert the persisted widget filter becomes `txn_type: ["income","expense"]`; unchecking all clears it to `undefined`. First read the existing editor-page test to reuse its render/open-popover helpers and its assertion style.

- [ ] **Step 2: Run test, verify it fails** → FAIL (radios, single value).

- [ ] **Step 3: Implement** — update the import on line 15 to keep `useId` only if still used (it is no longer needed — drop it, keep `useEffect`); add `asTxnTypeArray` + `TxnType` imports from `@/lib/reports/resolve` and `@/lib/reports/types`. Replace the usage block `:116-122` with:
```tsx
      <div className="flex flex-col gap-1">
        <TxnTypeCheckboxRow
          value={filters.txn_type}
          allowTransfer={allowTransfer}
          onChange={(txn_type) => onChange({ ...filters, txn_type })}
        />
      </div>
```
Replace `TxnTypeRadioRow` (`:177-228`) with:
```tsx
function TxnTypeCheckboxRow({
  value,
  allowTransfer,
  onChange,
}: {
  value: TxnType[] | undefined;
  allowTransfer: boolean;
  onChange: (next: TxnType[] | undefined) => void;
}) {
  const selected = asTxnTypeArray(value) ?? [];
  const choices: Array<{ value: TxnType; label: string }> = [
    { value: "income", label: "Income" },
    { value: "expense", label: "Expense" },
    // ``transfer`` is a transactions-only concept; omit it for sources
    // whose ``type`` can't be a transfer (recurring / accounts).
    ...(allowTransfer
      ? ([{ value: "transfer", label: "Transfer" }] as const)
      : []),
  ];
  // Self-heal: if a persisted ``transfer`` survives on a non-transactions
  // source (where the checkbox is hidden), strip it once. Depending on the
  // boolean keeps the effect from re-firing after the value settles.
  const hasIllegalTransfer = !allowTransfer && selected.includes("transfer");
  useEffect(() => {
    if (hasIllegalTransfer) {
      const cleaned = selected.filter((t) => t !== "transfer");
      onChange(cleaned.length > 0 ? cleaned : undefined);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasIllegalTransfer, onChange]);

  function toggle(t: TxnType) {
    const next = selected.includes(t)
      ? selected.filter((x) => x !== t)
      : [...selected, t];
    onChange(next.length > 0 ? next : undefined);
  }

  return (
    <>
      <div className="text-xs text-text-secondary">Transaction type</div>
      <div className="flex flex-wrap gap-3 text-xs text-text-secondary">
        {choices.map((c) => (
          <label key={c.value} className="flex items-center gap-1">
            <input
              type="checkbox"
              aria-label={`Widget transaction type ${c.label}`}
              checked={selected.includes(c.value)}
              onChange={() => toggle(c.value)}
            />
            <span>{c.label}</span>
          </label>
        ))}
      </div>
    </>
  );
}
```
Note: the "Any" choice is removed — none-checked = Any (no filter). Confirm `useId` import is dropped if unused.

- [ ] **Step 4: Run test, verify it passes** → PASS. Then `... npx tsc --noEmit` → clean.

- [ ] **Step 5: Commit**
```bash
git add frontend/components/reports/config/FilterEditor.tsx frontend/tests/app/reports-editor-page.test.tsx
git commit -m "feat(reports): txn_type multi-select checkboxes replace single-select radio"
```

---

### Task 5: Template draft + transfer-prune use arrays

**Files:**
- Modify: `frontend/lib/reports/draft.ts:35`
- Modify: `frontend/components/reports/config/useWidgetMutations.ts:153-181` (`finalizeFilters`, esp. `:158-159`)
- Test: `frontend/tests/components/reports/config/useWidgetMutations.test.ts(x)` (locate; else add a focused test)

**Interfaces:**
- Consumes: `asTxnTypeArray`.

- [ ] **Step 1: Write the failing test** — for `finalizeFilters` (or the exported mutation it backs): given a non-transactions dataset and `filters.txn_type: ["expense","transfer"]`, the persisted result has `txn_type: ["expense"]`; given `["transfer"]` on a non-transactions dataset, `txn_type` is dropped entirely; given a transactions dataset, `["expense","transfer"]` is preserved. First read the file to see whether `finalizeFilters` is exported/testable directly or only via a hook; if only via the hook, assert through the hook's save path as existing tests do.

- [ ] **Step 2: Run test, verify it fails** → FAIL (current code compares `pruned.txn_type === "transfer"`).

- [ ] **Step 3: Implement** — `draft.ts:35` → `filters: { txn_type: ["expense"] },`. In `useWidgetMutations.ts` replace the transfer-strip (`:158-159`) with array logic:
```ts
      if (dataset !== "transactions") {
        const kept = asTxnTypeArray(pruned.txn_type)?.filter((t) => t !== "transfer");
        if (kept && kept.length > 0) {
          pruned = { ...pruned, txn_type: kept };
        } else if (pruned.txn_type !== undefined) {
          const { txn_type: _drop, ...rest } = pruned;
          pruned = rest;
        }
      }
```
Add the `asTxnTypeArray` import. Adjust to the file's exact variable names (`pruned`/`rest`) after reading it.

- [ ] **Step 4: Run test, verify it passes** → PASS. `... npx tsc --noEmit` → clean.

- [ ] **Step 5: Commit**
```bash
git add frontend/lib/reports/draft.ts frontend/components/reports/config/useWidgetMutations.ts frontend/tests/components/reports/config/useWidgetMutations.test.*
git commit -m "feat(reports): txn_type template + non-transactions transfer-prune use arrays"
```

---

### Task 6: Full-suite verification + push

- [ ] **Step 1: Full vitest suite** — `docker compose exec frontend npx vitest run` → all green. Investigate any failure referencing `txn_type` (likely a stale single-value assertion) and fix.
- [ ] **Step 2: Lint + types** — `docker compose exec frontend npx tsc --noEmit` and `docker compose exec frontend npx eslint .` → clean (no new errors).
- [ ] **Step 3: Push + PR** — push `feat/reports-multiselect-txn-type`; PR title (conventional-commits — it's the deploy gate): `feat(reports): multi-select transaction-type filter (checkboxes)`. Body: summary + "backend already supported `in`; FE-only" + back-compat note.

## Self-Review (done at plan-write time)
- **Spec coverage:** types→array (T1), control→checkbox (T4), resolver eq→in (T2), chip join (T3), back-compat string coercion (T1 helper, used in T2/T3/T5), "none = Any" (T2/T4), transfer transactions-only (T4 hide + T5 prune), template fix (T5), full-suite verify (T6). All W1a spec points covered.
- **Placeholders:** none — every code step shows code; test steps that must match existing harnesses say "read the file first" with the exact assertion to add.
- **Type consistency:** `asTxnTypeArray`, `TxnType`, `WidgetFilters.txn_type: TxnType[]`, `op:"in"` used consistently across tasks.
