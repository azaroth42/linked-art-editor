import { describe, expect, it } from "vitest";

import { loginPageUrl, providerLoginUrl, safeNext } from "./loginPaths.ts";

describe("safeNext", () => {
  it.each(["/", "/edit/abc", "/records/abc/history?x=1#top"])("keeps the local path %j", (path) => {
    expect(safeNext(path)).toBe(path);
  });

  it.each([
    null,
    undefined,
    "",
    "edit/abc",
    "//evil.example/",
    "https://evil.example/",
    "/\\evil.example",
    "/edit\nabc",
    "/login",
    "/login?next=/login",
  ])("replaces %j with /", (value) => {
    expect(safeNext(value)).toBe("/");
  });
});

describe("loginPageUrl", () => {
  it("carries where to return to", () => {
    expect(loginPageUrl("/edit/abc?x=1")).toBe("/login?next=%2Fedit%2Fabc%3Fx%3D1");
  });

  it("omits next for the home page", () => {
    expect(loginPageUrl("/")).toBe("/login");
  });
});

describe("providerLoginUrl", () => {
  it("starts the API's login with the provider and a safe next", () => {
    expect(providerLoginUrl("github", "/edit/abc")).toBe("/auth/login/github?next=%2Fedit%2Fabc");
    expect(providerLoginUrl("oidc", "//evil.example")).toBe("/auth/login/oidc?next=%2F");
  });

  it("escapes the provider id", () => {
    expect(providerLoginUrl("a/b?c", "/")).toBe("/auth/login/a%2Fb%3Fc?next=%2F");
  });
});
