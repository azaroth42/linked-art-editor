import type { ReactNode } from "react";

import type { Permission } from "./permissions.ts";
import { useAuth } from "./useAuth.ts";

interface RequirePermissionProps {
  /** The permission, or every one of the permissions, the user must hold. */
  permission: Permission | readonly Permission[];
  /** Rendered instead of `children` when a permission is missing (default: nothing). */
  fallback?: ReactNode;
  children: ReactNode;
}

/**
 * Render `children` only when the signed-in user holds the permission(s). This only hides UI
 * the user could not use: the backend checks every request (403 `LAE-AUTH-001`) and stays the
 * authority, so never rely on this wrapper for security.
 */
export function RequirePermission({
  permission,
  fallback = null,
  children,
}: RequirePermissionProps) {
  const { can } = useAuth();
  const required: readonly Permission[] =
    typeof permission === "string" ? [permission] : permission;
  return can(...required) ? children : fallback;
}
