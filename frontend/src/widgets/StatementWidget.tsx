import type { WidgetProps } from "./environment.ts";
import { TextNodeList } from "./TextNodeList.tsx";

/**
 * `statement`: `LinguisticObject` nodes (`referred_to_by[LinguisticObject]`): content, language
 * and statement type from the slot's Value Set, edited like names.
 */
export function StatementWidget(props: WidgetProps) {
  return <TextNodeList {...props} />;
}
