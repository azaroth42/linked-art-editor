import { expect, test } from "@playwright/test";

import { login } from "./auth.ts";
import { expectNoAxeViolations } from "./axe.ts";

const RECORD_ID = "0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b";

// Every SPA route gets an axe check (conventions §5). `/` is covered in home.spec.ts.
const SIGNED_IN_ROUTES = [
  { path: `/edit/${RECORD_ID}`, heading: "Edit record", title: "Edit record – Laertes" },
  {
    path: `/records/${RECORD_ID}/history`,
    heading: "Revision history",
    title: "Revision history – Laertes",
  },
  { path: "/no/such/page", heading: "Page not found", title: "Page not found – Laertes" },
];

for (const route of SIGNED_IN_ROUTES) {
  test(`${route.path} renders and has no axe violations`, async ({ page }) => {
    await login(page, route.path);
    await expect(page.getByRole("heading", { level: 1, name: route.heading })).toBeVisible();
    await expect(page).toHaveTitle(route.title);
    await expectNoAxeViolations(page);
  });
}

test("/login offers sign-in to an anonymous visitor and has no axe violations", async ({
  page,
}) => {
  // The API runs with the dev user, so every request is signed in; answer /auth/me as the API
  // does for a request without a session.
  await page.route("**/auth/me", (route) =>
    route.fulfill({
      status: 401,
      json: { code: "LAE-AUTH-002", path: "/auth/me", params: {} },
    }),
  );
  // The e2e API has no identity provider configured; offer OIDC and GitHub as a server with both
  // would (docs/dev-setup.md).
  await page.route("**/auth/providers", (route) =>
    route.fulfill({
      json: [
        { id: "oidc", display_name: "OpenID Connect" },
        { id: "github", display_name: "GitHub" },
      ],
    }),
  );
  await page.goto(`/edit/${RECORD_ID}`);
  await expect(page).toHaveURL(`/login?next=%2Fedit%2F${RECORD_ID}`);
  await expect(page.getByRole("heading", { level: 1, name: "Sign in" })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Sign in with your organization’s account" }),
  ).toHaveAttribute("href", `/auth/login/oidc?next=%2Fedit%2F${RECORD_ID}`);
  await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveAttribute(
    "href",
    `/auth/login/github?next=%2Fedit%2F${RECORD_ID}`,
  );
  await expectNoAxeViolations(page);
});

test("the API lists its sign-in providers through the preview server's proxy", async ({ page }) => {
  // In CI the API has none; a reused local server may have some (docs/dev-setup.md).
  const response = await page.request.get("/auth/providers");
  expect(response.status()).toBe(200);
  const providers = (await response.json()) as unknown[];
  expect(Array.isArray(providers)).toBe(true);
  for (const provider of providers) {
    expect(provider).toEqual({ id: expect.any(String), display_name: expect.any(String) });
  }
});
