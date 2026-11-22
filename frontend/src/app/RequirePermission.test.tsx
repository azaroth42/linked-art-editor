import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EDITOR } from "../api/testing.ts";
import { AuthContext, type AuthState } from "./authContext.ts";
import { hasPermissions, type Permission } from "./permissions.ts";
import { RequirePermission } from "./RequirePermission.tsx";

function authState(user: typeof EDITOR | null): AuthState {
  return {
    status: user === null ? "anonymous" : "authenticated",
    user,
    error: null,
    retry: () => undefined,
    can: (...permissions: Permission[]) => hasPermissions(user?.permissions, permissions),
    logout: () => Promise.resolve(),
    loggingOut: false,
  };
}

function renderAs(user: typeof EDITOR | null, permission: Permission | Permission[]) {
  render(
    <AuthContext.Provider value={authState(user)}>
      <RequirePermission permission={permission} fallback={<span data-testid="fallback" />}>
        <span data-testid="guarded" />
      </RequirePermission>
    </AuthContext.Provider>,
  );
}

describe("RequirePermission", () => {
  afterEach(cleanup);

  it("renders its children when the user holds the permission", () => {
    renderAs(EDITOR, "publish");
    expect(screen.queryByTestId("guarded")).not.toBeNull();
    expect(screen.queryByTestId("fallback")).toBeNull();
  });

  it("renders the fallback when the permission is missing", () => {
    renderAs(EDITOR, "manage_users");
    expect(screen.queryByTestId("guarded")).toBeNull();
    expect(screen.queryByTestId("fallback")).not.toBeNull();
  });

  it("requires every permission of a list", () => {
    renderAs(EDITOR, ["publish", "delete"]);
    expect(screen.queryByTestId("guarded")).not.toBeNull();
    cleanup();
    renderAs(EDITOR, ["publish", "manage_system"]);
    expect(screen.queryByTestId("guarded")).toBeNull();
  });

  it("renders the fallback when signed out", () => {
    renderAs(null, "edit_draft");
    expect(screen.queryByTestId("guarded")).toBeNull();
    expect(screen.queryByTestId("fallback")).not.toBeNull();
  });

  it("renders nothing by default", () => {
    const { container } = render(
      <AuthContext.Provider value={authState(null)}>
        <RequirePermission permission="publish">
          <span data-testid="guarded" />
        </RequirePermission>
      </AuthContext.Provider>,
    );
    expect(container.innerHTML).toBe("");
  });
});
