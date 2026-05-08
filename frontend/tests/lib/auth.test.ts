import { hasPlatformPermission, isAdmin, isOwner, isSuperadmin } from "@/lib/auth";
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
    password_set: true,
    allow_manual_balance_adjustment: false,
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

describe("hasPlatformPermission", () => {
  it("returns false for null/undefined users", () => {
    expect(hasPlatformPermission(null, "roles.manage")).toBe(false);
    expect(hasPlatformPermission(undefined, "roles.manage")).toBe(false);
  });

  it("returns true for superadmin regardless of permissions array", () => {
    const user = makeUser({ is_superadmin: true });
    expect(hasPlatformPermission(user, "roles.manage")).toBe(true);
    expect(hasPlatformPermission(user, "anything.else")).toBe(true);
  });

  it("returns true for non-superadmin whose permissions array includes the key", () => {
    const user = makeUser({ permissions: ["roles.manage", "audit.view"] });
    expect(hasPlatformPermission(user, "roles.manage")).toBe(true);
    expect(hasPlatformPermission(user, "audit.view")).toBe(true);
  });

  it("returns false for non-superadmin whose permissions array omits the key", () => {
    const user = makeUser({ permissions: ["audit.view"] });
    expect(hasPlatformPermission(user, "roles.manage")).toBe(false);
  });

  it("returns false for non-superadmin with empty or undefined permissions", () => {
    expect(hasPlatformPermission(makeUser({ permissions: [] }), "roles.manage")).toBe(false);
    expect(hasPlatformPermission(makeUser({ permissions: undefined }), "roles.manage")).toBe(
      false,
    );
    expect(hasPlatformPermission(makeUser(), "roles.manage")).toBe(false);
  });
});
