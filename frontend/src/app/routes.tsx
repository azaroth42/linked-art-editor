/**
 * The SPA's routes. All share the `AppShell` layout; every page except the login page and the
 * 404 page needs a signed-in user. The public reader (`/id/{uuid}`) is server-rendered by the API
 * (decision D8) and never routed here.
 */
import type { RouteObject } from "react-router";

import { EditorPage } from "../editor/EditorPage.tsx";
import { HistoryPage } from "../editor/HistoryPage.tsx";
import { AppShell } from "./AppShell.tsx";
import { HomePage } from "./HomePage.tsx";
import { LOGIN_PATH } from "./loginPaths.ts";
import { LoginPage } from "./LoginPage.tsx";
import { NotFoundPage } from "./NotFoundPage.tsx";
import { RequireAuth } from "./RequireAuth.tsx";

export const routes: RouteObject[] = [
  {
    element: <AppShell />,
    children: [
      {
        path: "/",
        element: (
          <RequireAuth>
            <HomePage />
          </RequireAuth>
        ),
      },
      {
        path: "/edit/:id",
        element: (
          <RequireAuth>
            <EditorPage />
          </RequireAuth>
        ),
      },
      {
        path: "/records/:id/history",
        element: (
          <RequireAuth>
            <HistoryPage />
          </RequireAuth>
        ),
      },
      { path: LOGIN_PATH, element: <LoginPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
