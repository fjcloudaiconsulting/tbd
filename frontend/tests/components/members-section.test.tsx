import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import MembersSection from "@/components/settings/MembersSection";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});


describe("MembersSection", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("renders members + invitations and submits invite to the right endpoint", async () => {
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === "/api/v1/orgs/members") {
        return Promise.resolve([
          { id: 1, username: "owner", email: "o@a.io", role: "owner", is_active: true },
          { id: 2, username: "alice", email: "a@a.io", role: "member", is_active: true },
        ]);
      }
      if (url === "/api/v1/orgs/invitations" && (!opts || opts.method !== "POST")) {
        return Promise.resolve([
          { id: 10, email: "pending@a.io", role: "member", created_at: "", expires_at: "", inviter_username: "owner", status: "pending" },
        ]);
      }
      if (url === "/api/v1/orgs/invitations" && opts?.method === "POST") {
        return Promise.resolve({});
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<MembersSection currentUserId={1} currentRole="owner" />);

    await screen.findByText("alice");
    await screen.findByText("o@a.io");
    await screen.findByText(/pending@a.io/);

    fireEvent.change(screen.getByLabelText(/Invite by email/i), {
      target: { value: "new@a.io" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send invitation/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/orgs/invitations",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ email: "new@a.io", role: "member" }),
        }),
      );
    });
  });

  it("hides invite form and remove buttons for plain MEMBER role", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/orgs/members") {
        return Promise.resolve([
          { id: 5, username: "boss", email: "boss@a.io", role: "owner", is_active: true },
          { id: 6, username: "self", email: "self@a.io", role: "member", is_active: true },
        ]);
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<MembersSection currentUserId={6} currentRole="member" />);

    await screen.findByText("boss");
    expect(
      screen.queryByLabelText(/Invite by email/i),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Remove boss/i }),
    ).toBeNull();
  });

  it("revokes a pending invitation when admin clicks Revoke", async () => {
    let invitationsList: { id: number; email: string; role: string }[] = [
      { id: 99, email: "todelete@a.io", role: "member" },
    ];
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === "/api/v1/orgs/members") {
        return Promise.resolve([
          { id: 1, username: "admin", email: "ad@a.io", role: "admin", is_active: true },
        ]);
      }
      if (url === "/api/v1/orgs/invitations" && (!opts || opts.method !== "POST")) {
        return Promise.resolve(
          invitationsList.map((i) => ({ ...i, created_at: "", expires_at: "", inviter_username: "admin", status: "pending" })),
        );
      }
      if (url === "/api/v1/orgs/invitations/99" && opts?.method === "DELETE") {
        invitationsList = [];
        return Promise.resolve(undefined);
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<MembersSection currentUserId={1} currentRole="admin" />);

    const revoke = await screen.findByRole("button", {
      name: /Revoke invitation for todelete@a.io/i,
    });
    fireEvent.click(revoke);

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/orgs/invitations/99",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
  });
});
