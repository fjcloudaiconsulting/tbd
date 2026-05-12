import React from "react";
import { render, waitFor } from "@testing-library/react";

import LandingAuthRedirect from "@/components/landing/LandingAuthRedirect";
import { useAuth } from "@/components/auth/AuthProvider";
import { useRouter } from "next/navigation";

vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

const useAuthMock = vi.mocked(useAuth);
const useRouterMock = vi.mocked(useRouter);

type AuthLike = ReturnType<typeof useAuth>;

function makeAuth(overrides: Partial<AuthLike> = {}): AuthLike {
  return {
    user: null,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
    ...overrides,
  } as AuthLike;
}

describe("<LandingAuthRedirect />", () => {
  const replace = vi.fn();

  beforeEach(() => {
    replace.mockReset();
    useRouterMock.mockReturnValue({
      replace,
      push: vi.fn(),
      back: vi.fn(),
      forward: vi.fn(),
      refresh: vi.fn(),
      prefetch: vi.fn(),
    } as ReturnType<typeof useRouter>);
  });

  it("renders null (no DOM output)", () => {
    useAuthMock.mockReturnValue(makeAuth());
    const { container } = render(<LandingAuthRedirect />);
    expect(container.firstChild).toBeNull();
  });

  it("does NOT redirect while auth is loading", async () => {
    useAuthMock.mockReturnValue(makeAuth({ loading: true }));
    render(<LandingAuthRedirect />);
    // microtask flush
    await Promise.resolve();
    expect(replace).not.toHaveBeenCalled();
  });

  it("does NOT redirect anonymous visitors", async () => {
    useAuthMock.mockReturnValue(makeAuth());
    render(<LandingAuthRedirect />);
    await Promise.resolve();
    expect(replace).not.toHaveBeenCalled();
  });

  it("redirects authenticated visitors to /dashboard", async () => {
    useAuthMock.mockReturnValue(
      makeAuth({
        user: {
          id: 1,
          username: "alice",
          email: "a@b.test",
          first_name: "Alice",
          last_name: "T",
          phone: null,
          avatar_url: null,
          email_verified: true,
          role: "owner",
          org_id: 1,
          org_name: "Test Org",
          billing_cycle_day: 1,
          is_superadmin: false,
          is_active: true,
          mfa_enabled: false,
          password_set: true,
          allow_manual_balance_adjustment: false,
          subscription_status: null,
          subscription_plan: null,
          trial_end: null,
        } as never,
      }),
    );
    render(<LandingAuthRedirect />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
  });

  it("redirects to /setup when needsSetup is true", async () => {
    useAuthMock.mockReturnValue(makeAuth({ needsSetup: true }));
    render(<LandingAuthRedirect />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/setup"));
  });
});
