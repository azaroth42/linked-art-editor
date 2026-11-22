import { type ReactNode, useCallback, useMemo } from "react";

import { useLogout, useMe } from "../api/hooks/auth.ts";
import { AuthContext, type AuthState, type AuthStatus } from "./authContext.ts";
import { hasPermissions, type Permission } from "./permissions.ts";

/**
 * Reads the caller from `GET /auth/me` and provides it, with `can(...)` and `logout()`, through
 * {@link AuthContext} (`useAuth`). Signing in is a full-page navigation to `/auth/login`
 * (`LoginPage`); with `LAE_DEV_USER` set on the API every request is already that user.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const me = useMe();
  const logoutMutation = useLogout();
  const user = me.data ?? null;
  const { refetch } = me;
  const { mutateAsync } = logoutMutation;

  // A failed background refetch keeps the last answer: only a first failure is an error state.
  let status: AuthStatus;
  if (me.data !== undefined) {
    status = user === null ? "anonymous" : "authenticated";
  } else if (me.isError) {
    status = "error";
  } else {
    status = "loading";
  }

  const retry = useCallback(() => void refetch(), [refetch]);
  const logout = useCallback(async () => {
    await mutateAsync();
  }, [mutateAsync]);
  const can = useCallback(
    (...permissions: Permission[]) => hasPermissions(user?.permissions, permissions),
    [user],
  );

  const value = useMemo<AuthState>(
    () => ({
      status,
      user: status === "authenticated" ? user : null,
      error: me.error,
      retry,
      can,
      logout,
      loggingOut: logoutMutation.isPending,
    }),
    [status, user, me.error, retry, can, logout, logoutMutation.isPending],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
