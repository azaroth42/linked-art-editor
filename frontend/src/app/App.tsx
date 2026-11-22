import { useTranslation } from "react-i18next";

/** Placeholder route for `/`: renders one translated string. Step 1.16 adds the router and shell. */
export function App() {
  const { t } = useTranslation();
  return (
    <main data-testid="app-root">
      <h1>{t("app.title")}</h1>
    </main>
  );
}
