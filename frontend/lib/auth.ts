import type { User } from "@/lib/types";

export function isAdmin(user: User): boolean {
  return user.role === "owner" || user.role === "admin" || user.is_superadmin;
}

export function isOwner(user: User): boolean {
  return user.role === "owner" || user.is_superadmin;
}

export function isSuperadmin(user: User): boolean {
  return user.is_superadmin;
}

// Forward-compatible platform-permission gate. Superadmin short-circuits
// to true; otherwise check the optional permissions array. Today /me does
// not return permissions, so non-superadmin users always resolve to false
// (matches existing isSuperadmin behavior). The day backend /me starts
// returning a permissions array, FE call sites pick it up automatically.
export function hasPlatformPermission(
  user: User | null | undefined,
  permission: string,
): boolean {
  if (!user) return false;
  if (user.is_superadmin) return true;
  return Array.isArray(user.permissions) && user.permissions.includes(permission);
}
