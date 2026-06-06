import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import TransactionForm from "@/components/floating/TransactionForm";
import { apiFetch } from "@/lib/api";
import { todayISO } from "@/lib/format";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const ACCT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 1000,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const CAT = {
  id: 10,
  name: "Groceries",
  type: "expense" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "groceries",
  is_system: false,
  transaction_count: 0,
};

// Description autocomplete fires a GET to
// /api/v1/transactions/suggestions/descriptions whenever the user types
// >= 2 chars. The tests below mock apiFetch generically with {} which
// the autocomplete safely falls back to (`data.suggestions ?? []`).
// Helpers focus assertions on the POST /api/v1/transactions call so
// debounced suggestion fetches don't affect call counts.
type Call = Parameters<typeof apiFetch>;
function postCalls(mock: ReturnType<typeof vi.mocked<typeof apiFetch>>): Call[] {
  return mock.mock.calls.filter(
    (call) =>
      call[0] === "/api/v1/transactions" &&
      (call[1] as { method?: string } | undefined)?.method === "POST",
  ) as Call[];
}

describe("TransactionForm", () => {
  it("renders the empty state when there are no accounts or categories", () => {
    render(
      <TransactionForm
        accounts={[]}
        categories={[]}
        onSaved={() => {}}
      />,
    );
    expect(screen.getByText(/Create at least one account/i)).toBeInTheDocument();
  });

  it("submits a valid transaction and calls onSaved (default Save closes the panel)", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();
    const onTransactionAdded = vi.fn();

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={onSaved}
        onTransactionAdded={onTransactionAdded}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Groceries Aldi" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "12.34" },
    });
    // Account defaults from the is_default fixture; category from prop.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledTimes(1);
    });
    expect(onTransactionAdded).toHaveBeenCalledTimes(1);
    const posts = postCalls(apiFetchMock);
    expect(posts).toHaveLength(1);
    const [path, options] = posts[0];
    expect(path).toBe("/api/v1/transactions");
    expect(options?.method).toBe("POST");
    const body = JSON.parse(String(options?.body));
    expect(body.description).toBe("Groceries Aldi");
    expect(body.amount).toBe("12.34");
    expect(body.account_id).toBe(ACCT.id);
    expect(body.category_id).toBe(CAT.id);
    expect(body.type).toBe("expense");
    expect(body.status).toBe("settled");
  });

  it("Save and add new keeps the panel open and clears description and amount", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={onSaved}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "First" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "9.99" },
    });

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /save and add new/i }),
      );
    });

    await waitFor(() => {
      expect(postCalls(apiFetchMock)).toHaveLength(1);
    });
    // Panel must stay open: onSaved must NOT have fired.
    expect(onSaved).not.toHaveBeenCalled();
    // Form must be cleared.
    const desc = screen.getByLabelText("Description") as HTMLInputElement;
    const amount = screen.getByLabelText("Amount") as HTMLInputElement;
    expect(desc.value).toBe("");
    expect(amount.value).toBe("");
  });

  it("flips status to pending when a credit-card account is selected", () => {
    const CREDIT = {
      ...ACCT,
      id: 2,
      name: "Visa",
      account_type_slug: "credit_card",
      is_default: false,
    };
    render(
      <TransactionForm
        accounts={[ACCT, CREDIT]}
        categories={[CAT]}
        onSaved={() => {}}
      />,
    );
    const status = screen.getByLabelText("Status") as HTMLSelectElement;
    expect(status.value).toBe("settled");
    fireEvent.change(screen.getByLabelText("Account"), {
      target: { value: String(CREDIT.id) },
    });
    expect(status.value).toBe("pending");
  });

  it("respects defaultAccountId when provided", () => {
    const SAVINGS = { ...ACCT, id: 99, name: "Savings", is_default: false };
    render(
      <TransactionForm
        accounts={[ACCT, SAVINGS]}
        categories={[CAT]}
        defaultAccountId={SAVINGS.id}
        onSaved={() => {}}
      />,
    );
    const account = screen.getByLabelText("Account") as HTMLSelectElement;
    expect(account.value).toBe(String(SAVINGS.id));
  });

  it("Save and add new respects native validation: blank required fields skip the network call", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={onSaved}
      />,
    );

    // Description and amount are blank: the form is invalid. The
    // browser's requestSubmit() must skip onSubmit, so apiFetch must
    // never be called.
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /save and add new/i }),
      );
    });

    // Give any pending microtasks a chance to flush.
    await new Promise((r) => setTimeout(r, 0));

    expect(apiFetchMock).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("Save and add new submits when fields are valid and resets description and amount while keeping the panel open", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();
    const onTransactionAdded = vi.fn();

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={onSaved}
        onTransactionAdded={onTransactionAdded}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Coffee" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "3.50" },
    });

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /save and add new/i }),
      );
    });

    await waitFor(() => {
      expect(postCalls(apiFetchMock)).toHaveLength(1);
    });
    expect(onTransactionAdded).toHaveBeenCalledTimes(1);
    // Panel stays open.
    expect(onSaved).not.toHaveBeenCalled();
    // Description and amount reset; account preserved.
    const desc = screen.getByLabelText("Description") as HTMLInputElement;
    const amount = screen.getByLabelText("Amount") as HTMLInputElement;
    const account = screen.getByLabelText("Account") as HTMLSelectElement;
    expect(desc.value).toBe("");
    expect(amount.value).toBe("");
    expect(account.value).toBe(String(ACCT.id));
  });

  // Expected settlement date for pending transactions (PR #197 parity).
  // The canonical /transactions form exposes a settled_date input only
  // when status=pending, validates settled_date >= date, and only sends
  // the field on pending creates with a value set. The FAB quick-entry
  // form must match.
  describe("expected settlement date (pending parity with #197)", () => {
    it("does not render the expected settlement date input when status is settled", () => {
      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );
      // Default account is checking, so status defaults to settled.
      expect(
        screen.queryByLabelText(/expected settlement date/i),
      ).not.toBeInTheDocument();
    });

    it("renders the expected settlement date input when status flips to pending", () => {
      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );
      fireEvent.change(screen.getByLabelText("Status"), {
        target: { value: "pending" },
      });
      expect(
        screen.getByLabelText(/expected settlement date/i),
      ).toBeInTheDocument();
    });

    it("rejects submit when settled_date < date and does not call apiFetch", async () => {
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockResolvedValue({} as never);

      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "Bad date" },
      });
      fireEvent.change(screen.getByLabelText("Amount"), {
        target: { value: "5.00" },
      });
      fireEvent.change(screen.getByLabelText("Status"), {
        target: { value: "pending" },
      });
      const dateInput = screen.getByLabelText("Date") as HTMLInputElement;
      fireEvent.change(dateInput, { target: { value: "2026-05-10" } });
      const settledDateInput = screen.getByLabelText(
        /expected settlement date/i,
      ) as HTMLInputElement;
      fireEvent.change(settledDateInput, { target: { value: "2026-05-01" } });

      // Submit via the form rather than the Save click. jsdom's HTML5
      // validation on the date input's `min` attribute can pre-empt the
      // submit handler when triggered through the button; dispatching
      // `submit` exercises the same code path React listens to and lets
      // the JS-level cross-field guard run, mirroring the canonical
      // /transactions form's test pattern (PR #197).
      const form = screen
        .getByRole("button", { name: /^Save$/i })
        .closest("form")!;
      await act(async () => {
        fireEvent.submit(form);
      });

      // Inline error rendered, no network call attempted.
      expect(
        await screen.findByText(
          /must be on or after the transaction date/i,
        ),
      ).toBeInTheDocument();
      expect(apiFetchMock).not.toHaveBeenCalled();
    });

    it("includes settled_date in the payload when status=pending and a value is set", async () => {
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockResolvedValue({} as never);

      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "CC charge" },
      });
      fireEvent.change(screen.getByLabelText("Amount"), {
        target: { value: "42.00" },
      });
      fireEvent.change(screen.getByLabelText("Status"), {
        target: { value: "pending" },
      });
      fireEvent.change(screen.getByLabelText("Date"), {
        target: { value: "2026-05-10" },
      });
      fireEvent.change(screen.getByLabelText(/expected settlement date/i), {
        target: { value: "2026-05-15" },
      });

      const form = screen
        .getByRole("button", { name: /^Save$/i })
        .closest("form")!;
      await act(async () => {
        fireEvent.submit(form);
      });

      await waitFor(() => {
        expect(postCalls(apiFetchMock)).toHaveLength(1);
      });
      const [, options] = postCalls(apiFetchMock)[0];
      const body = JSON.parse(String(options?.body));
      expect(body.status).toBe("pending");
      expect(body.settled_date).toBe("2026-05-15");
    });

    it("omits settled_date from the payload when status=settled", async () => {
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockResolvedValue({} as never);

      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "Cash" },
      });
      fireEvent.change(screen.getByLabelText("Amount"), {
        target: { value: "10.00" },
      });
      // Status stays at the default ("settled") for the checking
      // fixture; do not touch the settled-date field, it shouldn't even
      // be rendered.

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
      });

      await waitFor(() => {
        expect(postCalls(apiFetchMock)).toHaveLength(1);
      });
      const [, options] = postCalls(apiFetchMock)[0];
      const body = JSON.parse(String(options?.body));
      expect(body.status).toBe("settled");
      expect(body).not.toHaveProperty("settled_date");
    });

    it("omits settled_date when status=pending but no value is set", async () => {
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockResolvedValue({} as never);

      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "No expected" },
      });
      fireEvent.change(screen.getByLabelText("Amount"), {
        target: { value: "1.00" },
      });
      fireEvent.change(screen.getByLabelText("Status"), {
        target: { value: "pending" },
      });
      // Settled-date field is rendered but left blank.

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
      });

      await waitFor(() => {
        expect(postCalls(apiFetchMock)).toHaveLength(1);
      });
      const [, options] = postCalls(apiFetchMock)[0];
      const body = JSON.parse(String(options?.body));
      expect(body.status).toBe("pending");
      expect(body).not.toHaveProperty("settled_date");
    });

    it("Save and add new clears the settled_date alongside description and amount", async () => {
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockResolvedValue({} as never);

      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "First pending" },
      });
      fireEvent.change(screen.getByLabelText("Amount"), {
        target: { value: "9.99" },
      });
      fireEvent.change(screen.getByLabelText("Status"), {
        target: { value: "pending" },
      });
      fireEvent.change(screen.getByLabelText(/expected settlement date/i), {
        target: { value: "2026-12-31" },
      });

      await act(async () => {
        fireEvent.click(
          screen.getByRole("button", { name: /save and add new/i }),
        );
      });

      await waitFor(() => {
        expect(postCalls(apiFetchMock)).toHaveLength(1);
      });
      // The settled-date control's render is gated on status==="pending".
      // clearForm() leaves status alone (it re-derives from the account
      // selection), so for the checking-default fixture the field
      // un-renders. Either path is equivalent: the persisted React state
      // is cleared and any subsequent pending submit re-starts blank.
      // To assert the cleared state, flip status back to pending.
      fireEvent.change(screen.getByLabelText("Status"), {
        target: { value: "pending" },
      });
      const settledDateAfter = screen.getByLabelText(
        /expected settlement date/i,
      ) as HTMLInputElement;
      expect(settledDateAfter.value).toBe("");
    });
  });

  describe("description autocomplete wiring", () => {
    // Regression: the AppShell quick-add panel rendered a plain <input>
    // instead of DescriptionAutocomplete, so typing into Description
    // never fetched suggestions. Operator hit this on the daily-driver
    // path. These tests pin the wiring (combobox role + fetch fire +
    // category auto-fill on pick) so it can't silently regress again.

    it("renders the Description field as a combobox (autocomplete is wired)", () => {
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockResolvedValue({} as never);

      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      const desc = screen.getByLabelText("Description");
      expect(desc.getAttribute("role")).toBe("combobox");
      expect(desc.getAttribute("aria-autocomplete")).toBe("list");
    });

    it("fetches description suggestions when the user types >= 2 chars", async () => {
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockImplementation((path: string) => {
        if (path.startsWith("/api/v1/transactions/suggestions/descriptions")) {
          return Promise.resolve({ suggestions: [] }) as never;
        }
        return Promise.resolve({}) as never;
      });

      render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "HBO" },
      });

      await waitFor(() => {
        const suggestionCalls = apiFetchMock.mock.calls.filter((call) =>
          String(call[0]).startsWith(
            "/api/v1/transactions/suggestions/descriptions",
          ),
        );
        expect(suggestionCalls.length).toBeGreaterThanOrEqual(1);
        const url = new URL(String(suggestionCalls[0][0]), "http://localhost");
        expect(url.searchParams.get("q")).toBe("HBO");
        expect(url.searchParams.get("type")).toBe("expense");
      });
    });

    it("auto-fills the category from the picked suggestion when category is empty", async () => {
      const SUGGESTION = {
        description: "HBO Max",
        category_id: CAT.id,
        category_name: CAT.name,
        use_count: 4,
        last_used: "2026-05-10",
      };
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockImplementation((path: string) => {
        if (path.startsWith("/api/v1/transactions/suggestions/descriptions")) {
          return Promise.resolve({ suggestions: [SUGGESTION] }) as never;
        }
        return Promise.resolve({}) as never;
      });

      // No defaultCategoryId so the user starts with an empty category.
      const { container } = render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          onSaved={() => {}}
        />,
      );

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "HB" },
      });

      const option = await screen.findByRole("option", { name: /HBO Max/i });
      fireEvent.mouseDown(option);

      // (a) Description fills (proxy assertion, preserved).
      await waitFor(() => {
        const desc = screen.getByLabelText("Description") as HTMLInputElement;
        expect(desc.value).toBe("HBO Max");
      });

      // (b) Category state pins directly to the picked suggestion's
      // category_id. CategorySelect renders the chosen category name
      // in its visible <input id="fab-tx-category">, and exposes the
      // numeric id via the adjacent hidden <input name="...-value">.
      // Both must agree with SUGGESTION.category_id.
      await waitFor(() => {
        const categoryInput = container.querySelector(
          "#fab-tx-category",
        ) as HTMLInputElement;
        expect(categoryInput.value).toBe(CAT.name);
      });
      const hiddenCategory = container.querySelector(
        'input[name="fab-tx-category-value"]',
      ) as HTMLInputElement;
      expect(hiddenCategory.value).toBe(String(SUGGESTION.category_id));
    });

    it("does NOT overwrite an already-chosen category when picking a description suggestion", async () => {
      // Boundary case for the auto-fill contract in
      // TransactionForm.tsx:349. When categoryId !== "" at the moment
      // of pick, the suggestion's category_id MUST NOT clobber the
      // user's choice. Mirrors the canonical /transactions form's
      // "optional pre-populate" rule.
      const OTHER_CAT = {
        id: 77,
        name: "Subscriptions",
        type: "expense" as const,
        parent_id: null,
        parent_name: null,
        description: null,
        slug: "subscriptions",
        is_system: false,
        transaction_count: 0,
      };
      const SUGGESTION = {
        description: "HBO Max",
        category_id: OTHER_CAT.id, // Different from defaultCategoryId.
        category_name: OTHER_CAT.name,
        use_count: 4,
        last_used: "2026-05-10",
      };
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockImplementation((path: string) => {
        if (path.startsWith("/api/v1/transactions/suggestions/descriptions")) {
          return Promise.resolve({ suggestions: [SUGGESTION] }) as never;
        }
        return Promise.resolve({}) as never;
      });

      // Pre-select CAT (id=10) via defaultCategoryId — categoryId is
      // NOT empty at the time of pick.
      const { container } = render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT, OTHER_CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      // Sanity: category starts at CAT (Groceries), not OTHER_CAT.
      const hiddenBefore = container.querySelector(
        'input[name="fab-tx-category-value"]',
      ) as HTMLInputElement;
      expect(hiddenBefore.value).toBe(String(CAT.id));

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "HB" },
      });
      const option = await screen.findByRole("option", { name: /HBO Max/i });
      fireEvent.mouseDown(option);

      // Description still fills (proves the pick fired).
      await waitFor(() => {
        const desc = screen.getByLabelText("Description") as HTMLInputElement;
        expect(desc.value).toBe("HBO Max");
      });

      // Category MUST still be CAT — the suggestion's OTHER_CAT.id
      // was rejected by the `categoryId === ""` guard.
      const hiddenAfter = container.querySelector(
        'input[name="fab-tx-category-value"]',
      ) as HTMLInputElement;
      expect(hiddenAfter.value).toBe(String(CAT.id));
      const categoryInput = container.querySelector(
        "#fab-tx-category",
      ) as HTMLInputElement;
      expect(categoryInput.value).toBe(CAT.name);
    });
  });

  it("treats PUT /tags failure as partial success: no duplicate POST on retry", async () => {
    // Regression for PR #326 review: the two-step write (POST then
    // PUT /tags) must not re-POST the base transaction when the tag
    // attach fails. Previously a tag-PUT failure left the form open
    // with all fields intact, and a second Save click double-created.
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();

    const onSaved = vi.fn();
    const onTransactionAdded = vi.fn();
    const onWarning = vi.fn();

    // 201 for the base POST; 500 for PUT /tags.
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/transactions" && init?.method === "POST") {
        return { id: 42 } as never;
      }
      if (url === "/api/v1/transactions/42/tags") {
        throw new Error("tag attach failed");
      }
      return {} as never;
    });

    const { container } = render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={onSaved}
        onTransactionAdded={onTransactionAdded}
        onWarning={onWarning}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Groceries Aldi" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "12.34" },
    });
    // Add a tag chip so the PUT /tags arm fires.
    const tagInput = container.querySelector(
      "#fab-tx-tags",
    ) as HTMLInputElement;
    fireEvent.change(tagInput, { target: { value: "rent" } });
    fireEvent.keyDown(tagInput, { key: "Enter" });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    // (1) Panel closed via onSaved, list refresh fired via
    // onTransactionAdded, warning surfaced via onWarning.
    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledTimes(1);
    });
    expect(onTransactionAdded).toHaveBeenCalledTimes(1);
    expect(onWarning).toHaveBeenCalledTimes(1);
    expect(onWarning.mock.calls[0][0]).toMatch(/Transaction saved/);
    expect(onWarning.mock.calls[0][0]).toMatch(/tag attach failed/);

    // (2) Exactly ONE base POST. The whole point of this fix is no
    // duplicate base transaction on the tag-failure path.
    const basePosts = apiFetchMock.mock.calls.filter(
      ([url, init]) =>
        url === "/api/v1/transactions" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(basePosts).toHaveLength(1);
  });

  it("promotes the new transaction to recurring when Repeats is on", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/transactions" && init?.method === "POST") {
        return { id: 42 } as never;
      }
      return {} as never;
    });

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Rent" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "1200.00" },
    });
    fireEvent.click(screen.getByLabelText("Repeats"));
    fireEvent.change(screen.getByLabelText("Frequency"), {
      target: { value: "monthly" },
    });
    fireEvent.click(screen.getByLabelText("Auto-settle"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      expect(postCalls(apiFetchMock)).toHaveLength(1);
    });

    const promoteCalls = apiFetchMock.mock.calls.filter(
      ([url, init]) =>
        url === "/api/v1/transactions/42/promote-to-recurring" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(promoteCalls).toHaveLength(1);
    const promoteBody = JSON.parse(
      String((promoteCalls[0][1] as RequestInit | undefined)?.body),
    );
    expect(promoteBody.frequency).toBe("monthly");
    expect(promoteBody.auto_settle).toBe(true);
    expect(promoteBody.next_due_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);

    // Recurring is set up via promote-to-recurring (which links the source
    // row's recurring_id), NOT the legacy standalone POST /recurring that
    // left the source row unlinked and re-created a duplicate template on
    // a later edit. Guard against regressing to that path.
    const legacyRecurringCalls = apiFetchMock.mock.calls.filter(
      ([url]) => String(url) === "/api/v1/recurring",
    );
    expect(legacyRecurringCalls).toHaveLength(0);
  });

  it("bumps a back-dated recurring next_due_date forward to today", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/transactions" && init?.method === "POST") {
        return { id: 42 } as never;
      }
      return {} as never;
    });

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Rent" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "1200.00" },
    });
    // Back-date the transaction; the recurring next_due_date must still be
    // today-or-later (server guard), so it should be bumped to today.
    fireEvent.change(screen.getByLabelText("Date"), {
      target: { value: "2020-01-01" },
    });
    fireEvent.click(screen.getByLabelText("Repeats"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      expect(postCalls(apiFetchMock)).toHaveLength(1);
    });

    const promoteCalls = apiFetchMock.mock.calls.filter(
      ([url, init]) =>
        url === "/api/v1/transactions/42/promote-to-recurring" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(promoteCalls).toHaveLength(1);
    const promoteBody = JSON.parse(
      String((promoteCalls[0][1] as RequestInit | undefined)?.body),
    );
    expect(promoteBody.next_due_date).toBe(todayISO());
  });

  it("does not promote when Repeats is off", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/transactions" && init?.method === "POST") {
        return { id: 42 } as never;
      }
      return {} as never;
    });

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Rent" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "1200.00" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      expect(postCalls(apiFetchMock)).toHaveLength(1);
    });

    const promoteCalls = apiFetchMock.mock.calls.filter(([url]) =>
      String(url).includes("/promote-to-recurring"),
    );
    expect(promoteCalls).toHaveLength(0);
  });
});
