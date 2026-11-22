/**
 * The user every e2e request runs as: `playwright.config.ts` starts `lae api` with
 * `LAE_DEV_USER=<email>:<role>` (docs/dev-setup.md §1), so there is no identity provider.
 */
export const E2E_DEV_USER = { email: "e2e-editor@example.org", role: "Editor" } as const;
