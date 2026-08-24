import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import AdminUserDetailPage from "@/app/admin/users/[user_id]/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { EMAIL_RECOVERY_NOTICE } from "@/lib/email-recovery";

// TBD-362 fences F13/F14/F15 for the operator email-recovery UI on
// /admin/users/[user_id]. Design: specs/2026-08-23-tbd-362-admin-email-recovery.md
//
// ⚠ Every control is queried with getByRole(role, { name }) and never with
// getByLabelText: getByLabelText also matches the wrapping <label> element,
// so it passes against a field that has no accessible name at all and
// therefore cannot fence one.

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/users/42",
  useParams: () => ({ user_id: "42" }),
}));

const SUPERADMIN = {
  id: 1,
  username: "root",
  email: "root@platform.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Platform",
  billing_cycle_day: 1,
  is_superadmin: true,
  is_active: true,
  mfa_enabled: false,
};

// The population this endpoint exists for: signed up with a typo, never
// verified, still active, not a superadmin.
const LOCKED_OUT = {
  id: 42,
  email: "ada@acme.oi",
  username: "ada",
  display_name: "Ada Lovelace",
  is_superadmin: false,
  is_active: true,
  email_verified: false,
  pending_email: null as string | null,
  mfa_enabled: false,
  password_set: true,
  password_changed_at: null,
  sessions_invalidated_at: null,
  onboarded_at: null,
  created_at: "2026-04-15T10:00:00",
  phone: null,
  orgs: [{ org_id: 10, name: "Acme Co", role: "owner" }],
  recent_audit_events: [],
};

type Detail = typeof LOCKED_OUT;

describe("TBD-362 admin email recovery UI", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  // Mutable so a successful POST/DELETE can be followed by the card
  // revalidating against the NEW server state, which is the behaviour the
  // "modal closes on success and the card revalidates" requirement names.
  let served: Detail;
  let posted: unknown[];

  function serveApi() {
    apiFetchMock.mockImplementation((async (
      path: string,
      options?: { method?: string; body?: string },
    ) => {
      const method = (options?.method ?? "GET").toUpperCase();
      if (method === "GET") return served;
      if (method === "POST" && path.endsWith("/email-change")) {
        posted.push(JSON.parse(options?.body ?? "{}"));
        served = { ...served, pending_email: "ada@acme.io" };
        return {
          user_id: 42,
          email: served.email,
          email_verified: false,
          pending_email: "ada@acme.io",
          previous_pending_email: null,
        };
      }
      if (method === "DELETE" && path.endsWith("/pending-email")) {
        served = { ...served, pending_email: null };
        return { cleared: true };
      }
      throw new Error(`unexpected call ${method} ${path}`);
    }) as never);
  }

  function signIn(user: Record<string, unknown>) {
    useAuthMock.mockReturnValue({
      user: user as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  }

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
    served = { ...LOCKED_OUT };
    posted = [];
    signIn(SUPERADMIN);
    serveApi();
  });

  async function openModal() {
    const trigger = await screen.findByRole("button", {
      name: /change email address/i,
    });
    trigger.focus();
    fireEvent.click(trigger);
    return { trigger, dialog: await screen.findByRole("dialog") };
  }

  // ── F13 ────────────────────────────────────────────────────────────

  it("F13: the Account recovery card is absent when the actor lacks users.reset_credentials", async () => {
    // A non-superadmin whose platform permissions carry users.view but NOT
    // users.reset_credentials. hasPlatformPermission is the only gate.
    signIn({
      ...SUPERADMIN,
      is_superadmin: false,
      permissions: ["users.view"],
    });

    render(<AdminUserDetailPage />);

    await screen.findByRole("heading", { name: "Ada Lovelace" });
    expect(screen.queryByTestId("account-recovery")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /change email address/i }),
    ).not.toBeInTheDocument();
  });

  it("F13: the card renders for an actor holding users.reset_credentials", async () => {
    signIn({
      ...SUPERADMIN,
      is_superadmin: false,
      permissions: ["users.view", "users.reset_credentials"],
    });

    render(<AdminUserDetailPage />);

    expect(await screen.findByTestId("account-recovery")).toBeInTheDocument();
  });

  it("F13: submit is gated until the normalized addresses match and the reason is >= 4 chars", async () => {
    render(<AdminUserDetailPage />);
    const { dialog } = await openModal();

    const submit = within(dialog).getByRole("button", {
      name: /send confirmation link/i,
    });
    const newEmail = within(dialog).getByRole("textbox", {
      name: /^new email address$/i,
    });
    const confirm = within(dialog).getByRole("textbox", {
      name: /^confirm new email address$/i,
    });
    const reason = within(dialog).getByRole("textbox", { name: /^reason$/i });

    // Nothing typed.
    expect(submit).toBeDisabled();

    // Addresses match after normalization (case + surrounding space), but
    // the reason is still too short. Kills a gate that only checks the
    // addresses.
    fireEvent.change(newEmail, { target: { value: "  Ada@Acme.io " } });
    fireEvent.change(confirm, { target: { value: "ada@acme.io" } });
    fireEvent.change(reason, { target: { value: "typ" } });
    expect(submit).toBeDisabled();

    // Reason long enough, but the addresses differ. Kills a gate that only
    // checks the reason.
    fireEvent.change(reason, { target: { value: "typo at signup" } });
    fireEvent.change(confirm, { target: { value: "adaa@acme.io" } });
    expect(submit).toBeDisabled();
    // The disabled state is paired with a VISIBLE reason, not a title alone.
    expect(within(dialog).getByTestId("submit-blocked-reason")).toHaveTextContent(
      /match/i,
    );

    // Both satisfied -> enabled, on values that differ only by case and
    // whitespace. A byte-equality check fails this leg.
    fireEvent.change(confirm, { target: { value: "ada@acme.io" } });
    await waitFor(() => expect(submit).toBeEnabled());

    // And it posts the normalized value with the trimmed reason.
    fireEvent.click(submit);
    await waitFor(() => {
      expect(posted).toEqual([
        {
          new_email: "ada@acme.io",
          new_email_confirm: "ada@acme.io",
          reason: "typo at signup",
        },
      ]);
    });
    // Modal closes on success and the card revalidates without a reload.
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByTestId("identity-pending-email")).toHaveTextContent(
        "ada@acme.io",
      ),
    );
  });

  it("F13: submit is refused when the new address equals the current one", async () => {
    render(<AdminUserDetailPage />);
    const { dialog } = await openModal();

    const submit = within(dialog).getByRole("button", {
      name: /send confirmation link/i,
    });
    fireEvent.change(
      within(dialog).getByRole("textbox", { name: /^new email address$/i }),
      { target: { value: "Ada@Acme.OI" } },
    );
    fireEvent.change(
      within(dialog).getByRole("textbox", { name: /^confirm new email address$/i }),
      { target: { value: "ada@acme.oi" } },
    );
    fireEvent.change(within(dialog).getByRole("textbox", { name: /^reason$/i }), {
      target: { value: "typo at signup" },
    });

    expect(submit).toBeDisabled();
    expect(within(dialog).getByTestId("submit-blocked-reason")).toHaveTextContent(
      /already the address on this account/i,
    );
  });

  it("F13: the 'does not verify the account' ruling is on BOTH the card and the modal, unreworded", async () => {
    render(<AdminUserDetailPage />);

    const cardCopy = await screen.findByTestId("account-recovery");
    expect(cardCopy).toHaveTextContent(EMAIL_RECOVERY_NOTICE);
    // Named explicitly so a silent re-word is a failure, not a passing
    // paraphrase: the sentence is the ruling, rendered.
    expect(cardCopy).toHaveTextContent(
      /This does not verify the account\. A confirmation link is sent to the new address; the account stays locked out until the user opens it\./,
    );
    // The Google-SSO consequence rides with it.
    expect(cardCopy).toHaveTextContent(/Google/);

    const { dialog } = await openModal();
    expect(dialog).toHaveTextContent(EMAIL_RECOVERY_NOTICE);
    expect(dialog).toHaveTextContent(
      /This does not verify the account\. A confirmation link is sent to the new address; the account stays locked out until the user opens it\./,
    );
    expect(dialog).toHaveTextContent(/Google/);
  });

  it("F13: the action is blocked with a visible reason on a verified / superadmin / inactive target", async () => {
    for (const [patch, pattern] of [
      [{ email_verified: true }, /already verified/i],
      [{ is_superadmin: true }, /superadmin/i],
      [{ is_active: false }, /inactive|reactivate/i],
    ] as const) {
      served = { ...LOCKED_OUT, ...patch };
      const view = render(<AdminUserDetailPage />);
      const trigger = await screen.findByRole("button", {
        name: /change email address/i,
      });
      expect(trigger).toBeDisabled();
      expect(screen.getByTestId("recovery-blocked-reason")).toHaveTextContent(
        pattern,
      );
      view.unmount();
    }
  });

  // ── F14 ────────────────────────────────────────────────────────────

  it("F14: with a live pending_email the card renders a cancel control and clicking it calls DELETE", async () => {
    served = { ...LOCKED_OUT, pending_email: "ada@acme.io" };

    render(<AdminUserDetailPage />);

    // The identity card names the claim, as a labelled dt/dd pair rather
    // than a bare chip.
    const pendingRow = await screen.findByTestId("identity-pending-email");
    expect(pendingRow).toHaveTextContent("Pending email");
    expect(pendingRow).toHaveTextContent("ada@acme.io");

    const cancel = await screen.findByRole("button", {
      name: /cancel pending email change/i,
    });
    fireEvent.click(cancel);

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/users/42/pending-email",
        expect.objectContaining({ method: "DELETE" }),
      );
    });

    // And the card revalidates: the claim is gone without a reload.
    // Asserted on the identity row rather than on the button's absence --
    // the button's label becomes "Cancelling…" while the request is in
    // flight, so a name-based absence check would pass spuriously during
    // that window even if the claim never cleared.
    await waitFor(() =>
      expect(screen.getByTestId("identity-pending-email")).toHaveTextContent(
        "—",
      ),
    );
    expect(
      screen.queryByRole("button", { name: /cancel pending email change/i }),
    ).not.toBeInTheDocument();
  });

  it("F14: the identity card renders an em dash when there is no claim", async () => {
    render(<AdminUserDetailPage />);
    const pendingRow = await screen.findByTestId("identity-pending-email");
    expect(pendingRow).toHaveTextContent("—");
  });

  it("F14: Resend reopens the modal prefilled with the live claim", async () => {
    served = { ...LOCKED_OUT, pending_email: "ada@acme.io" };

    render(<AdminUserDetailPage />);

    const resend = await screen.findByRole("button", {
      name: /resend confirmation link/i,
    });
    fireEvent.click(resend);

    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("textbox", { name: /^new email address$/i }),
    ).toHaveValue("ada@acme.io");
  });

  // ── F15 ────────────────────────────────────────────────────────────

  describe("F15: the modal traps focus and restores it", () => {
    // jsdom never lays anything out, so HTMLElement.offsetParent is always
    // null and useFocusTrap's visibility filter discards EVERY focusable
    // element. Give offsetParent a browser-shaped value for the duration of
    // this block.
    //
    // ⚠ THIS SHIM PREVENTS A FALSE **RED**, NOT A FALSE GREEN -- an earlier
    // comment here said the opposite, and that is the sentence a future
    // reader would trust when deciding the shim can go. Measured: with
    // offsetParent forced to null and the implementation CORRECT, the
    // wrap-around assertion FAILS, because the hook's filter empties its
    // candidate list, takes the `preventDefault(); return` branch, and focus
    // never moves off the last element. Without the shim this block is
    // unusable, not merely weak.
    let original: PropertyDescriptor | undefined;
    beforeAll(() => {
      original = Object.getOwnPropertyDescriptor(
        HTMLElement.prototype,
        "offsetParent",
      );
      Object.defineProperty(HTMLElement.prototype, "offsetParent", {
        configurable: true,
        get(this: HTMLElement) {
          return this.parentElement;
        },
      });
    });
    afterAll(() => {
      if (original) {
        Object.defineProperty(HTMLElement.prototype, "offsetParent", original);
      } else {
        // @ts-expect-error -- restoring jsdom's absent property
        delete HTMLElement.prototype.offsetParent;
      }
    });

    it("focuses the first field on open, wraps Tab, closes on Escape and restores focus to the trigger", async () => {
      render(<AdminUserDetailPage />);
      const { trigger, dialog } = await openModal();

      const newEmail = within(dialog).getByRole("textbox", {
        name: /^new email address$/i,
      });
      // Initial focus lands on the first editable field, not on whatever
      // the DOM happens to put first.
      expect(document.activeElement).toBe(newEmail);

      const focusables = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => !el.hasAttribute("disabled"));
      expect(focusables.length).toBeGreaterThan(1);
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      expect(first).toBe(newEmail);

      // Tab off the last focusable wraps back to the first.
      last.focus();
      fireEvent.keyDown(document, { key: "Tab" });
      expect(document.activeElement).toBe(first);

      // Shift+Tab off the first wraps to the last.
      fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
      expect(document.activeElement).toBe(last);

      // Escape closes, and focus returns to the control that opened it.
      fireEvent.keyDown(document, { key: "Escape" });
      await waitFor(() =>
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
      );
      expect(document.activeElement).toBe(trigger);
    });

    it("renders a failure inline with role=alert and keeps the modal open", async () => {
      apiFetchMock.mockImplementation((async (
        path: string,
        options?: { method?: string },
      ) => {
        const method = (options?.method ?? "GET").toUpperCase();
        if (method === "GET") return served;
        throw Object.assign(new Error("That address already belongs to another user."), {
          status: 409,
        });
      }) as never);

      render(<AdminUserDetailPage />);
      const { dialog } = await openModal();

      fireEvent.change(
        within(dialog).getByRole("textbox", { name: /^new email address$/i }),
        { target: { value: "taken@acme.io" } },
      );
      fireEvent.change(
        within(dialog).getByRole("textbox", {
          name: /^confirm new email address$/i,
        }),
        { target: { value: "taken@acme.io" } },
      );
      fireEvent.change(
        within(dialog).getByRole("textbox", { name: /^reason$/i }),
        { target: { value: "typo at signup" } },
      );
      fireEvent.click(
        within(dialog).getByRole("button", { name: /send confirmation link/i }),
      );

      const alert = await within(dialog).findByRole("alert");
      expect(alert).toHaveTextContent(/already belongs to another user/i);
      // Every failure here is correctable in place, so the modal stays up.
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
  });
});
