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
