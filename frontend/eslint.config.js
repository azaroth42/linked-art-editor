// ESLint flat config. Step 0.2 adds `i18next/no-literal-string` as an error for .tsx files.
import js from "@eslint/js";
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
);
