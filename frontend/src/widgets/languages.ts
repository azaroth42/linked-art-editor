/**
 * Languages a `Name` or statement can be given: BCP-47 tag ↔ AAT language concept.
 *
 * Linked Art states a text's language as a reference to an AAT language concept; the editor
 * offers BCP-47 tags (shown with `Intl.DisplayNames`) and writes the concept. The table is the
 * one the server uses to read tags back (`AAT_LANGUAGE_TAGS` in
 * `backend/src/lae/labels/langtags.py`, with its sources); keep the two equal.
 */

/** The AAT namespace of the concept URIs. */
export const AAT = "http://vocab.getty.edu/aat/";

/** `[BCP-47 tag, AAT concept id]`. */
const TAGS: readonly (readonly [string, string])[] = [
  ["af", "300387782"],
  ["sq", "300387803"],
  ["ar", "300387843"],
  ["hy", "300387870"],
  ["eu", "300387955"],
  ["be", "300387967"],
  ["bn", "300387971"],
  ["bg", "300388034"],
  ["ca", "300388072"],
  ["zh", "300388113"],
  ["hr", "300388185"],
  ["cs", "300388191"],
  ["da", "300388204"],
  ["nl", "300388256"],
  ["en", "300388277"],
  ["eo", "300388282"],
  ["et", "300388283"],
  ["fi", "300388299"],
  ["fr", "300388306"],
  ["gl", "300388327"],
  ["ka", "300388343"],
  ["de", "300388344"],
  ["he", "300388401"],
  ["hi", "300388412"],
  ["is", "300388449"],
  ["id", "300388460"],
  ["it", "300388474"],
  ["ja", "300388486"],
  ["ko", "300388633"],
  ["la", "300388693"],
  ["lv", "300388696"],
  ["lt", "300388723"],
  ["mk", "300388760"],
  ["mt", "300388796"],
  ["nn", "300388992"],
  ["pl", "300389109"],
  ["pt", "300389115"],
  ["ro", "300389157"],
  ["ru", "300389168"],
  ["sa", "300389205"],
  ["sr", "300389247"],
  ["sk", "300389290"],
  ["sl", "300389291"],
  ["es", "300389311"],
  ["sv", "300389336"],
  ["ta", "300389365"],
  ["th", "300389405"],
  ["bo", "300389410"],
  ["tr", "300389470"],
  ["uk", "300389489"],
  ["ur", "300389502"],
  ["vi", "300389525"],
  ["cy", "300389555"],
  ["yi", "300389602"],
  ["el", "300389734"],
  ["nb", "300391418"],
  ["oc", "300391532"],
];

/** AAT language concept URI by BCP-47 tag. */
export const LANGUAGE_URIS: ReadonlyMap<string, string> = new Map(
  TAGS.map(([tag, id]) => [tag, `${AAT}${id}`]),
);

/** BCP-47 tag by AAT language concept URI. */
export const LANGUAGE_TAGS: ReadonlyMap<string, string> = new Map(
  TAGS.map(([tag, id]) => [`${AAT}${id}`, tag]),
);
