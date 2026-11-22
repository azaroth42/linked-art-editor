import { createContext } from "react";

import type { Me } from "../api/hooks/auth.ts";
import type { Permission } from "./permissions.ts";

/**
 * - `loading`: `/auth/me` has not answered yet.
 * - `authenticated`: there is a user (session cookie, dev user, or bearer token).
 * - `anonymous`: `/auth/me` answered 401.
 * - `error`: `/auth/me` failed otherwise (server or network down); `retry` asks again.
 */
export type AuthStatus = "loading" | "authenticated" | "anonymous" | "error";

export interface AuthState {
  status: AuthStatus;
  /** The signed-in user; `null` unless `status` is `authenticated`. */
  user: Me | null;
  /** The failure behind `status === "error"`. */
  error: unknown;
  /** Fetch `/auth/me` again. */
  retry: () => void;
  /** Whether the user holds every named permission (always `false` when signed out). */
  can: (...permissions: Permission[]) => boolean;
  /** End the session (`POST /auth/logout`); every cached query is reset. */
  logout: () => Promise<void>;
  /** `logout` is in flight. */
  loggingOut: boolean;
}

export const AuthContext = createContext<AuthState | null>(null);
