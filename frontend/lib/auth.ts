import type { User } from "@/lib/types";

export function isAdmin(user: User): boolean {
  return user.role === "owner" || user.role === "admin" || user.is_superadmin;
}
