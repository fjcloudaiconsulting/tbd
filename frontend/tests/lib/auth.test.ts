import { isAdmin, isOwner, isSuperadmin } from "@/lib/auth";
import type { User } from "@/lib/types";


function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1,
    username: "alice",
    email: "alice@example.com",
    first_name: "Alice",
    last_name: "Tester",
    phone: null,
    avatar_url: null,
    email_verified: true,
    role: "member",
    org_id: 1,
    org_name: "Test Org",
    billing_cycle_day: 1,
    is_superadmin: false,
    is_active: true,
    mfa_enabled: false,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
    ...overrides,
  };
}


describe("frontend auth helpers", () => {
  it("treats owners and admins as admins", () => {
    expect(isAdmin(makeUser({ role: "owner" }))).toBe(true);
    expect(isAdmin(makeUser({ role: "admin" }))).toBe(true);
    expect(isAdmin(makeUser({ role: "member" }))).toBe(false);
  });

  it("lets superadmins bypass role checks", () => {
    const user = makeUser({ role: "member", is_superadmin: true });

    expect(isAdmin(user)).toBe(true);
    expect(isOwner(user)).toBe(true);
    expect(isSuperadmin(user)).toBe(true);
  });
});
