/** Test data for the validation drawer and the Publish dialog. */
import type { UiProfile } from "../../api/hooks/editing.ts";
import type { Diagnostic, RecordSchema } from "../../api/hooks/publishing.ts";
import { slot } from "../../widgets/testing.tsx";

/** The slots of `profiles/ui/HumanMadeObject.yaml`, with one advanced section for the details. */
export const PROFILE: UiProfile = {
  class: "HumanMadeObject",
  version: 1,
  sections: [
    {
      id: "identification",
      label: { en: "Identification" },
      advanced: false,
      slots: [
        slot({ id: "title", path: "identified_by[Name]", widget: "langtext", required: true }),
        slot({ id: "identifiers", path: "identified_by[Identifier]", widget: "identifier" }),
        slot({ id: "type_of_work", path: "classified_as", widget: "classification" }),
      ],
    },
    {
      id: "production",
      label: { en: "Production" },
      advanced: false,
      slots: [
        slot({ id: "creator", path: "produced_by.carried_out_by", widget: "entity_ref" }),
        slot({ id: "production_date", path: "produced_by.timespan", widget: "edtf" }),
        slot({ id: "production_place", path: "produced_by.took_place_at", widget: "entity_ref" }),
      ],
    },
    {
      id: "physical",
      label: { en: "Physical" },
      advanced: true,
      slots: [
        slot({ id: "materials", path: "made_of", widget: "classification" }),
        slot({ id: "dimensions", path: "dimension", widget: "dimension" }),
      ],
    },
  ],
};

/** A diagnostic as the server sends it. */
export function finding(
  code: string,
  path: string,
  severity: Diagnostic["severity"] = "violation",
  params: Record<string, unknown> = {},
): Diagnostic {
  return { code, path, severity, params, message_en: `${code} at ${path}` };
}

const BASE = "https://linked.art/api/1.0/schema/";

/** `lae.validation.schema_map.SCHEMA_BY_TYPE`, as the endpoint answers it. */
export const SCHEMA_BY_TYPE: Readonly<Record<string, string>> = {
  HumanMadeObject: "object.json",
  Person: "person.json",
  Group: "group.json",
  Place: "place.json",
  Type: "concept.json",
  Currency: "concept.json",
  Language: "concept.json",
  Material: "concept.json",
  MeasurementUnit: "concept.json",
  Set: "set.json",
  DigitalObject: "digital.json",
  LinguisticObject: "text.json",
  VisualItem: "image.json",
  PropositionalObject: "abstract.json",
  Activity: "event.json",
  Event: "event.json",
  Period: "event.json",
};

const SCHEMA_FILES = import.meta.glob<Record<string, unknown>>(
  "../../../../profiles/schema/*.json",
  { eager: true, import: "default" },
);

function schemaFile(name: string): Record<string, unknown> {
  const contents = SCHEMA_FILES[`../../../../profiles/schema/${name}`];
  if (contents === undefined) {
    throw new Error(`no schema file ${name}`);
  }
  return contents;
}

/** The answer of `GET /api/profiles/schema/{class}` for `recordClass`. */
export function servedSchema(recordClass: string): RecordSchema {
  const entry = SCHEMA_BY_TYPE[recordClass] ?? "";
  return {
    class: recordClass,
    entry: BASE + entry,
    schemas: { [BASE + entry]: schemaFile(entry), [`${BASE}core.json`]: schemaFile("core.json") },
  };
}
