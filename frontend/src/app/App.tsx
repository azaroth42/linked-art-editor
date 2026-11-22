import { type QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router";

import { AuthProvider } from "./AuthProvider.tsx";

/** A router created from `routes` (`createBrowserRouter`, or `createMemoryRouter` in tests). */
type Router = Parameters<typeof RouterProvider>[0]["router"];

interface AppProps {
  router: Router;
  queryClient: QueryClient;
}

/**
 * The SPA: data fetching (TanStack Query), the signed-in user, and the routes. Render it under an
 * `I18nextProvider` and a `Suspense` boundary (`main.tsx`).
 */
export function App({ router, queryClient }: AppProps) {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  );
}
