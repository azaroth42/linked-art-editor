import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client.ts";

/**
 * The localized message of a failed request: the `errors` namespace's message for the backend's
 * `LAE-…` code with its params interpolated, or `null` when the failure has no such code (the
 * caller then says what failed in its own words). Server prose is never shown.
 */
export function useErrorMessage(): (error: unknown) => string | null {
  const { t } = useTranslation("errors");
  return useCallback(
    (error: unknown) =>
      error instanceof ApiError && error.code.startsWith("LAE-")
        ? t(`errors:${error.code}`, error.params)
        : null,
    [t],
  );
}
