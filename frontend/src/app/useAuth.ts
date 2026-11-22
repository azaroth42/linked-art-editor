import { useContext } from "react";

import { AuthContext, type AuthState } from "./authContext.ts";

/** The authentication state provided by `AuthProvider`. */
export function useAuth(): AuthState {
  const state = useContext(AuthContext);
  if (state === null) {
    throw new Error("useAuth must be used inside <AuthProvider>");
  }
  return state;
}
