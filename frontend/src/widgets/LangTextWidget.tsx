import type { WidgetProps } from "./environment.ts";
import { TextNodeList } from "./TextNodeList.tsx";

/**
 * `langtext`: names (`identified_by[Name]`), each with its `content` bound to a text area, a
 * BCP-47 language select, a display direction (`auto` by default) and its Name type from the
 * slot's Value Set in the bundle.
 */
export function LangTextWidget(props: WidgetProps) {
  return <TextNodeList {...props} />;
}
