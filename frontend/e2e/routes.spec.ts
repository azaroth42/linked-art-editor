import { expect, test } from "@playwright/test";

import { login } from "./auth.ts";
import { expectNoAxeViolations } from "./axe.ts";

const RECORD_ID = "0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b";

// Every SPA route gets an axe check (conventions §5). `/` is covered in home.spec.ts.
const SIGNED_IN_ROUTES = [
  { path: `/edit/${RECORD_ID}`, heading: "Edit record", title: "Edit record – Linked Art Editor" },
  {
    path: `/records/${RECORD_ID}/history`,
    heading: "Revision history",
    title: "Revision history – Linked Art Editor",
  },
  { path: "/no/such/page", heading: "Page not found", title: "Page not found – Linked Art Editor" },
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
  await page.goto(`/edit/${RECORD_ID}`);
  await expect(page).toHaveURL(`/login?next=%2Fedit%2F${RECORD_ID}`);
  await expect(page.getByRole("heading", { level: 1, name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Continue to sign-in" })).toHaveAttribute(
    "href",
    `/auth/login?next=%2Fedit%2F${RECORD_ID}`,
  );
  await expectNoAxeViolations(page);
});
