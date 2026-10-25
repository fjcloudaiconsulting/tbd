/**
 * TBD-657. Redeeming a verification link for an UNVERIFIED account needs the
 * account password. The page still auto-POSTs `{token}`; on the backend's
 * `password_required` it shows a password form and resubmits
 * `{token, password}`. Kills a page that treats every 4xx as final.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import VerifyEmailPage from "@/app/verify-email/page";
import { ApiResponseError } from "@/lib/api";

const apiFetchMock = vi.fn();
let tokenParam = "";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));
vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: () => ({ user: null }) }));
// Reads the live URL, as Next.js does: once the page strips ?token= from the
// address bar (FE-03), useSearchParams no longer returns it.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

const b64url = (o: object) =>
  btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
// An email claim whose JSON base64 contains "-" or "_" once url-encoded.
const jwt = (claims: object) => `h.${b64url(claims)}.s`;

const required = () =>
  new ApiResponseError(400, "Enter your password to confirm this email address.", "password_required");
const incorrect = () =>
  new ApiResponseError(400, "Enter your password to confirm this email address.", "password_incorrect");

const bodies = () =>
  apiFetchMock.mock.calls.map((c) => JSON.parse((c[1] as { body: string }).body));

beforeEach(() => {
  apiFetchMock.mockReset();
  tokenParam = jwt({ sub: "1", email: "victim?>>@example.com", type: "email_verify" });
});

// Lands on the page from the email link carrying the current `tokenParam`.
function renderPage() {
  window.history.replaceState(null, "", `/verify-email?${new URLSearchParams({ token: tokenParam })}`);
  return render(<VerifyEmailPage />);
}

async function reachForm() {
  apiFetchMock.mockRejectedValueOnce(required());
  renderPage();
  return screen.findByLabelText("Password");
}

describe("verify-email page, token in the URL (FE-03)", () => {
  it("drops the token from the address bar before the auto-POST", async () => {
    let urlAtPost = "";
    apiFetchMock.mockImplementationOnce(async () => {
      urlAtPost = window.location.href;
      return { detail: "Email verified" };
    });
    renderPage();
    await screen.findByText("Email verified!");
    expect(bodies()).toEqual([{ token: tokenParam }]);
    expect(urlAtPost).not.toContain("token=");
    expect(window.location.pathname).toBe("/verify-email");
    expect(window.location.search).toBe("");
  });
});

describe("verify-email page, password confirmation", () => {
  it("password_required renders the field, not 'verified' and not a dead end", async () => {
    const field = await reachForm();
    expect(field).toBeTruthy();
    expect(screen.queryByText(/Email verified/)).toBeNull();
    expect(screen.getByText("Enter your password to confirm this email address.")).toBeTruthy();
    expect(screen.getByText("Didn't sign up? You can close this page.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Forgot password?" }).getAttribute("href")).toBe(
      "/forgot-password",
    );
  });

  it("submit posts {token, password} and shows success", async () => {
    const field = await reachForm();
    apiFetchMock.mockResolvedValueOnce({ detail: "Email verified" });
    fireEvent.change(field, { target: { value: "hunter2hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await screen.findByText("Email verified!");
    expect(bodies()).toEqual([
      { token: tokenParam },
      { token: tokenParam, password: "hunter2hunter2" },
    ]);
  });

  it("Enter submits the form", async () => {
    const field = await reachForm();
    apiFetchMock.mockResolvedValueOnce({ detail: "Email verified" });
    fireEvent.change(field, { target: { value: "pw-pw-pw-1" } });
    fireEvent.submit(field.closest("form")!);
    await screen.findByText("Email verified!");
    expect(bodies()[1].password).toBe("pw-pw-pw-1");
  });

  it("password_incorrect shows an inline alert and keeps the field", async () => {
    const field = await reachForm();
    apiFetchMock.mockRejectedValueOnce(incorrect());
    fireEvent.change(field, { target: { value: "nope-nope-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/incorrect/i);
    expect(screen.getByLabelText("Password").getAttribute("aria-invalid")).toBe("true");
  });

  it("a 429 keeps the form with an inline message", async () => {
    const field = await reachForm();
    apiFetchMock.mockRejectedValueOnce(new ApiResponseError(429, "Too many requests"));
    fireEvent.change(field, { target: { value: "pw-pw-pw-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Password")).toBeTruthy();
  });

  it("a generic invalid-token 400 on resubmit is the final screen", async () => {
    const field = await reachForm();
    apiFetchMock.mockRejectedValueOnce(
      new ApiResponseError(400, "Invalid or expired verification token"),
    );
    fireEvent.change(field, { target: { value: "pw-pw-pw-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await screen.findByText("Invalid or expired verification token");
    expect(screen.queryByLabelText("Password")).toBeNull();
  });

  it("a 409 on resubmit goes straight to the final screen with its message", async () => {
    const field = await reachForm();
    apiFetchMock.mockRejectedValueOnce(new ApiResponseError(409, "That address is now in use."));
    fireEvent.change(field, { target: { value: "pw-pw-pw-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await screen.findByText("That address is now in use.");
    expect(screen.queryByLabelText("Password")).toBeNull();
  });

  it("an admin-initiated token shows the contact line, not the forgot link", async () => {
    tokenParam = jwt({ sub: "1", email: "a@example.com", admin_initiated: true });
    await reachForm();
    expect(screen.queryByRole("link", { name: "Forgot password?" })).toBeNull();
    const mail = screen.getByRole("link", { name: "hello@thebetterdecision.com" });
    expect(mail.getAttribute("href")).toBe("mailto:hello@thebetterdecision.com");
  });

  it("a garbage token still renders the form and posts", async () => {
    tokenParam = "garbage";
    const field = await reachForm();
    expect(screen.getByRole("link", { name: "Forgot password?" })).toBeTruthy();
    apiFetchMock.mockResolvedValueOnce({ detail: "Email verified" });
    fireEvent.change(field, { target: { value: "pw-pw-pw-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(bodies()[1]).toEqual({ token: "garbage", password: "pw-pw-pw-1" }));
  });

  it("the username input carries the token's email claim (base64url payload)", async () => {
    await reachForm();
    const u = document.querySelector('input[autocomplete="username"]') as HTMLInputElement;
    expect(u.value).toBe("victim?>>@example.com");
    expect(u.readOnly).toBe(true);
  });

  it.each([
    [400, "Invalid or expired verification token"],
    [409, "That address is now in use."],
  ])("an initial %i is the final screen with no Password field", async (status, msg) => {
    apiFetchMock.mockRejectedValueOnce(new ApiResponseError(status, msg));
    renderPage();
    await screen.findByText(msg);
    expect(screen.queryByLabelText("Password")).toBeNull();
  });
});
