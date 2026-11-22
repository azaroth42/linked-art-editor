// ESLint flat config.
import js from "@eslint/js";
import i18next from "eslint-plugin-i18next";
import jsxA11y from "eslint-plugin-jsx-a11y";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "playwright-report", "test-results"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.strict, jsxA11y.flatConfigs.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
    },
  },
  {
    // No user-facing literal strings in components (conventions §5): JSX text, string children,
    // and string attributes are errors unless the attribute is in the allow-list below. `to`
    // (router paths) and `permission` (RequirePermission, spec §8.2 names) are identifiers.
    files: ["**/*.tsx"],
    plugins: { i18next },
    rules: {
      "i18next/no-literal-string": [
        "error",
        {
          mode: "jsx-only",
          "jsx-attributes": {
            exclude: ["data-testid", "className", "href", "src", "type", "to", "permission"],
          },
        },
      ],
      // Gaps in the plugin: it treats `<a>` as an SVG tag and skips all its attributes, and it
      // never checks these announced ARIA attributes on HTML elements.
      "no-restricted-syntax": [
        "error",
        {
          selector:
            'JSXOpeningElement[name.name="a"] > JSXAttribute[name.name=/^(title|aria-label)$/] > Literal',
          message: "disallow literal string: user-facing text must come from t().",
        },
        {
          selector:
            "JSXAttribute[name.name=/^aria-(description|roledescription|valuetext|placeholder)$/] > Literal",
          message: "disallow literal string: user-facing text must come from t().",
        },
      ],
    },
  },
  {
    // Phase 0 spike pages are developer test harnesses: never routed from the app shell, never
    // shown to users, deleted once their phase has used the result. Their labels are not
    // translated. App code must not copy this exemption.
    files: ["src/spikes/**/*.tsx"],
    rules: { "i18next/no-literal-string": "off" },
  },
  {
    // Playwright tests and tool configs run in Node, not the browser.
    files: ["e2e/**/*.ts", "*.config.{ts,js}", "src/spikes/*.config.ts", "src/spikes/*.spec.ts"],
    languageOptions: { globals: globals.node },
  },
);
