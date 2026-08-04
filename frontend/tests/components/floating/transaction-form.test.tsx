import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import TransactionForm from "@/components/floating/TransactionForm";
import { apiFetch } from "@/lib/api";
import { advanceISO, todayISO } from "@/lib/format";

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

  it("surfaces both tag AND recurring partial-success failures in a single warning", async () => {
    // The parent (AppShellAddTransactionCta) stores a single warning string,
    // so a second onWarning call would overwrite the first. When BOTH the tag
    // attach and the promote-to-recurring fail, the component must emit ONE
    // combined warning so the user sees both problems.
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();

    const onWarning = vi.fn();

    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/transactions" && init?.method === "POST") {
        return { id: 42 } as never;
      }
      if (url === "/api/v1/transactions/42/tags") {
        throw new Error("tag attach failed");
      }
      if (url === "/api/v1/transactions/42/promote-to-recurring") {
        throw new Error("recurring setup failed");
      }
      return {} as never;
    });

    const { container } = render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={() => {}}
        onWarning={onWarning}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Rent" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "1200.00" },
    });
    const tagInput = container.querySelector(
      "#fab-tx-tags",
    ) as HTMLInputElement;
    fireEvent.change(tagInput, { target: { value: "rent" } });
    fireEvent.keyDown(tagInput, { key: "Enter" });
    fireEvent.click(screen.getByLabelText("Repeats"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    // Exactly ONE warning call, containing BOTH failure messages.
    await waitFor(() => {
      expect(onWarning).toHaveBeenCalledTimes(1);
    });
    expect(onWarning.mock.calls[0][0]).toMatch(/tag attach failed/);
    expect(onWarning.mock.calls[0][0]).toMatch(/recurring schedule/);
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

  // ── TBD-301: the client never rewrites the user's date ───────────────────
  //
  // This used to be "bumps a back-dated recurring next_due_date forward to
  // today", asserting the opposite. The floor it fenced was wrong: TBD-283
  // moved the server's lower bound off `today` and onto the start of the
  // org's CURRENT billing cycle, so for any org whose cycle does not begin
  // today the clamp was silently re-anchoring a series to a date the user
  // never picked, inside a window the API would have accepted.

  /** Fill the form with `date` + `frequency`, tick Repeats, Save, and return
   *  the `next_due_date` the promote call actually put on the wire. */
  async function promoteNextDue(
    date: string,
    frequency: string,
  ): Promise<string> {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/transactions" && init?.method === "POST") {
        return { id: 42 } as never;
      }
      return {} as never;
    });

    const { unmount } = render(
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
    fireEvent.change(screen.getByLabelText("Date"), { target: { value: date } });
    fireEvent.click(screen.getByLabelText("Repeats"));
    fireEvent.change(screen.getByLabelText("Frequency"), {
      target: { value: frequency },
    });

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
    unmount();
    return promoteBody.next_due_date as string;
  }

  it("sends a back-dated series' advanced date UNMODIFIED, never floored to today", async () => {
    // ⭐ FENCE (TBD-301). The load-bearing case: `2020-01-01` + monthly
    // advances to `2020-02-01`, which is genuinely behind today, so the
    // removed clamp DID fire on this fixture. A fixture where
    // `advanced >= today` would pass against the unfixed code and fence
    // nothing.
    const sent = await promoteNextDue("2020-01-01", "monthly");
    expect(sent).toBe("2020-02-01");
    // Stated separately because it names the exact wrong value: the old
    // clamp rewrote this to today.
    expect(sent).not.toBe(todayISO());
    expect(advanceISO("2020-01-01", "monthly")).toBe("2020-02-01");
  });

  it("CONTROL: a forward-dated series is also sent unmodified, so the fence above is not measuring a constant", async () => {
    // Same helper, same assertions shape, a date the clamp never touched.
    // If `promoteNextDue` were wired to something that cannot vary, this and
    // the fence above could not both hold.
    const sent = await promoteNextDue("2099-01-31", "monthly");
    expect(sent).toBe("2099-02-28");
    expect(sent).not.toBe(todayISO());
  });

  it("surfaces the server's frontier refusal AND says the transaction saved", async () => {
    // ⭐ FENCE (TBD-301). Removing the clamp means a back-dated promote can
    // now be refused by the server. That is acceptable ONLY because the
    // refusal reaches the user with its own text: the server's message names
    // the org-relative boundary, which is the one thing the client cannot
    // compute. Swallowing it, or replacing it with a generic string, leaves
    // the user with a saved transaction, no series, and no way out.
    const SERVER_MSG =
      "Next due date cannot be earlier than 2026-08-15, the start of the current billing cycle. Send a next_due_date on or after that date; a frequency change on a template whose schedule is already behind must carry one.";

    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/transactions" && init?.method === "POST") {
        return { id: 42 } as never;
      }
      if (url === "/api/v1/transactions/42/promote-to-recurring") {
        throw new Error(SERVER_MSG);
      }
      return {} as never;
    });

    const onWarning = vi.fn();
    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={() => {}}
        onWarning={onWarning}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Rent" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "1200.00" },
    });
    fireEvent.change(screen.getByLabelText("Date"), {
      target: { value: "2020-01-01" },
    });
    fireEvent.click(screen.getByLabelText("Repeats"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      expect(onWarning).toHaveBeenCalledTimes(1);
    });
    const warning = String(onWarning.mock.calls[0][0]);
    // The server's own sentence, verbatim, boundary date included.
    expect(warning).toContain(SERVER_MSG);
    expect(warning).toContain("2026-08-15");
    // And it is unambiguous that the transaction itself is on disk.
    expect(warning).toMatch(/Transaction saved/);
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

  // ── TBD-275: instalment count on the quick-add FAB ────────────────────────

  /** Render the FAB form, fill the required fields, tick Repeats, run the
   *  optional extra setup, submit, and hand back the parsed promote body
   *  (or null when no promote call was made). */
  async function submitWithRepeat(
    extra?: () => void,
  ): Promise<Record<string, unknown> | null> {
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
      target: { value: "Klarna" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "49.00" },
    });
    fireEvent.click(screen.getByLabelText("Repeats"));
    extra?.();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    const promoteCalls = apiFetchMock.mock.calls.filter(
      ([url, init]) =>
        url === "/api/v1/transactions/42/promote-to-recurring" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    if (promoteCalls.length === 0) return null;
    return JSON.parse(
      String((promoteCalls[0][1] as RequestInit | undefined)?.body),
    ) as Record<string, unknown>;
  }

  it("OMITS occurrence_count entirely when the payments field is blank", async () => {
    // FENCE. Blank means open-ended, which is what every repeat was before
    // TBD-275. `occurrence_count: null` and `occurrence_count: 0` are both
    // wrong on the wire -- the schema is `Optional[int] = Field(gt=0)`, so 0
    // is a 422 and null is a noisier spelling of absent.
    //
    // ⚠ `toBeUndefined()` alone would be GREEN against a body that sends
    // `null`, because `JSON.parse('{"occurrence_count":null}').occurrence_count`
    // is null, not undefined -- and `null == undefined`. The key-presence
    // assertion is the discriminating one.
    const body = await submitWithRepeat();
    expect(body).not.toBeNull();
    expect(Object.keys(body as object)).not.toContain("occurrence_count");
    // The rest of the promote body is untouched by this field.
    expect(body!.frequency).toBe("monthly");
  });

  it("threads a filled-in payments count through to the promote body as a number", async () => {
    // FENCE. The whole point of the field. A string "4" would 422 on some
    // shapes and is not what the schema declares, so the number-ness is
    // asserted, not just the value.
    const body = await submitWithRepeat(() => {
      fireEvent.change(screen.getByLabelText("Number of payments"), {
        target: { value: "4" },
      });
    });
    expect(body!.occurrence_count).toBe(4);
    expect(typeof body!.occurrence_count).toBe("number");
  });

  it("REJECTS zero and non-numeric payment counts before the transaction POST", async () => {
    // FENCE. Rejected client-side, and rejected EARLY: a count validated
    // between the POST and the promote would leave the user with a saved
    // transaction, no recurring series, and an error that reads like the save
    // failed. So the base POST must not happen at all.
    //
    // ⚠ "abc" is in this list on purpose and is why the input is
    // `type="text"`. A `type="number"` input COERCES it to the empty string,
    // which reads as blank, which means open-ended -- a 4-payment plan turned
    // into a forever plan with no message and no way for the user to know.
    // The same input shape is what makes this fence non-vacuous for "0" and
    // "2.5" too: with `min`/`step` on a number input, jsdom's native
    // constraint validation blocks the submit before the handler runs, so the
    // test would pass without any guard in the component at all.
    for (const bad of ["0", "-1", "2.5", "abc"]) {
      const apiFetchMock = vi.mocked(apiFetch);
      apiFetchMock.mockReset();
      apiFetchMock.mockImplementation(
        async (url: string, init?: RequestInit) => {
          if (url === "/api/v1/transactions" && init?.method === "POST") {
            return { id: 42 } as never;
          }
          return {} as never;
        },
      );

      const { unmount } = render(
        <TransactionForm
          accounts={[ACCT]}
          categories={[CAT]}
          defaultCategoryId={CAT.id}
          onSaved={() => {}}
        />,
      );

      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "Klarna" },
      });
      fireEvent.change(screen.getByLabelText("Amount"), {
        target: { value: "49.00" },
      });
      fireEvent.click(screen.getByLabelText("Repeats"));
      fireEvent.change(screen.getByLabelText("Number of payments"), {
        target: { value: bad },
      });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
      });

      expect(
        screen.getByText(/Number of payments must be a whole number/i),
        `value=${bad}`,
      ).toBeInTheDocument();
      // ⭐ Nothing was written. Not the transaction, not the series.
      expect(postCalls(apiFetchMock), `value=${bad}`).toHaveLength(0);
      unmount();
    }
  });

  it("sends the NEXT occurrence as next_due_date, not the transaction's own date", async () => {
    // FENCE. The default FAB entry is dated today and used to promote with
    // `next_due_date = today` -- the frontier landing ON the source row.
    // Generation's idempotency probe (`recurring_id == r.id AND date == due`,
    // no status term, no lower bound) matches that row and spends an
    // instalment for it, so a 4-payment plan delivered 3 while the UI read
    // "4 of 4". The frontier must be the transaction's date advanced by one
    // period.
    //
    // A FUTURE date, chosen when a today-floor still stood between this
    // assertion and the wire: with a back-dated row both the correct and the
    // broken implementation returned today, making the fence vacuous. TBD-301
    // removed that floor, so the date no longer has to be future for this to
    // discriminate -- but it stays future because the month-end clamp below
    // is the property under test and 2099-01-31 exercises it exactly. The
    // back-dated direction is now fenced by "sends a back-dated series'
    // advanced date UNMODIFIED, never floored to today" above.
    const future = "2099-01-31";
    const body = await submitWithRepeat(() => {
      fireEvent.change(screen.getByLabelText("Date"), {
        target: { value: future },
      });
    });
    // ⭐ Advanced, and month-end CLAMPED (2099 is not a leap year), matching
    // `relativedelta`. Not "2099-01-31", and not "2099-03-03".
    expect(body!.next_due_date).toBe("2099-02-28");
    expect(body!.next_due_date).not.toBe(future);
    expect(body!.next_due_date).toBe(advanceISO(future, "monthly"));
  });
});
