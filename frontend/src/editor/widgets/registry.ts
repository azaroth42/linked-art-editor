/**
 * The widget registry: UI Profile `widget` names (`profiles/ui/_schema.json`) → components.
 * `ProfileRenderer` resolves every slot through it.
 */
import type { ComponentType } from "react";

import type { ProfileSlot } from "../../api/hooks/editing.ts";
import { ClassificationWidget } from "../../widgets/ClassificationWidget.tsx";
import { DimensionWidget } from "../../widgets/DimensionWidget.tsx";
import { EdtfWidget } from "../../widgets/EdtfWidget.tsx";
import { EntityRefAutocomplete } from "../../widgets/EntityRefAutocomplete.tsx";
import type { WidgetProps } from "../../widgets/environment.ts";
import { IdentifierWidget } from "../../widgets/IdentifierWidget.tsx";
import { LangTextWidget } from "../../widgets/LangTextWidget.tsx";
import { ProductionWidget } from "../../widgets/ProductionWidget.tsx";
import { StatementWidget } from "../../widgets/StatementWidget.tsx";
import { TextWidget } from "../../widgets/TextWidget.tsx";

export type WidgetName = ProfileSlot["widget"];

/** Every widget name of the profile schema; `wkt` is a plain text field until it has its own. */
export const WIDGETS: Record<WidgetName, ComponentType<WidgetProps>> = {
  text: TextWidget,
  langtext: LangTextWidget,
  edtf: EdtfWidget,
  entity_ref: EntityRefAutocomplete,
  classification: ClassificationWidget,
  dimension: DimensionWidget,
  identifier: IdentifierWidget,
  statement: StatementWidget,
  wkt: TextWidget,
  production: ProductionWidget,
};

/** The component for a slot's widget. */
export function widgetFor(name: WidgetName): ComponentType<WidgetProps> {
  return WIDGETS[name];
}
