/**
 * Published records the Public Reader tests open at `/id/{uuid}` (Step 1.21).
 *
 * `globalSetup.ts` stores them with `scripts/seed_e2e_records.py` before the tests run (publish
 * arrives with Step 1.10). Their URIs are under {@link READER_BASE_URI}, which
 * `playwright.config.ts` also gives the e2e API as `LAE_BASE_URI`, so `/id/{uuid}` finds them and
 * links between them stay on the reader. The ids are fixed UUIDv7s; seeding replaces them.
 */

/** `LAE_BASE_URI` of the e2e API. */
export const READER_BASE_URI = "http://localhost:8000/id/";

export const KYLIX_ID = "01937a4e-0000-7000-8000-00000000e2e1";
export const PAINTER_ID = "01937a4e-0000-7000-8000-00000000e2e2";
export const IMAGE_ID = "01937a4e-0000-7000-8000-00000000e2e3";
export const MANIFEST_ID = "01937a4e-0000-7000-8000-00000000e2e4";

/** The IIIF Image API service of {@link IMAGE_ID}; the tests answer its requests themselves. */
export const IMAGE_SERVICE = "https://iiif.test/image/kylix";

/** The IIIF Manifest of {@link MANIFEST_ID}, painted by {@link IMAGE_SERVICE}. */
export const MANIFEST = "https://iiif.test/manifest.json";

/** The first Canvas of {@link MANIFEST}. */
export const MANIFEST_CANVAS = "https://iiif.test/canvas/1";

/** The size `info.json` reports for {@link IMAGE_SERVICE}. */
export const IMAGE_SIZE = { width: 1200, height: 900 } as const;

/** The region named by the image's access point URL. */
export const IMAGE_REGION = { x: 100, y: 50, width: 400, height: 300 } as const;

const CONTEXT = "https://linked.art/ns/v1/linked-art.json";
const AAT = "http://vocab.getty.edu/aat/";
const PRIMARY_NAME = { id: `${AAT}300404670`, type: "Type", _label: "Primary Name" };
const ENGLISH = { id: `${AAT}300388277`, type: "Language", _label: "English" };
const FRENCH = { id: `${AAT}300388306`, type: "Language", _label: "French" };

function name(content: string, language: object, primary = true): object {
  return {
    type: "Name",
    ...(primary ? { classified_as: [PRIMARY_NAME] } : {}),
    content,
    language: [language],
  };
}

const KYLIX = {
  "@context": CONTEXT,
  id: `${READER_BASE_URI}${KYLIX_ID}`,
  type: "HumanMadeObject",
  _label: "Red-figure kylix",
  identified_by: [
    name("Red-figure kylix", ENGLISH),
    name("Coupe à figures rouges", FRENCH),
    {
      type: "Identifier",
      classified_as: [{ id: `${AAT}300312355`, type: "Type", _label: "Accession Number" }],
      content: "1913.169",
    },
  ],
  classified_as: [{ id: `${AAT}300132254`, type: "Type", _label: "kylikes" }],
  produced_by: {
    type: "Production",
    carried_out_by: [{ id: `${READER_BASE_URI}${PAINTER_ID}`, type: "Person" }],
    timespan: {
      type: "TimeSpan",
      begin_of_the_begin: "-0479-01-01T00:00:00Z",
      end_of_the_end: "-0469-12-31T23:59:59Z",
    },
    took_place_at: [{ id: "https://example.org/place/athens", type: "Place", _label: "Athens" }],
  },
  made_of: [{ id: `${AAT}300010669`, type: "Material", _label: "terracotta" }],
  dimension: [
    {
      type: "Dimension",
      classified_as: [{ id: `${AAT}300055624`, type: "Type", _label: "diameter" }],
      value: 31.5,
      unit: { id: `${AAT}300379098`, type: "MeasurementUnit", _label: "centimeters" },
    },
  ],
  referred_to_by: [
    {
      type: "LinguisticObject",
      classified_as: [{ id: `${AAT}300435416`, type: "Type", _label: "Description" }],
      content: "A drinking cup painted in the red-figure technique.",
      language: [ENGLISH],
    },
  ],
};

const PAINTER = {
  "@context": CONTEXT,
  id: `${READER_BASE_URI}${PAINTER_ID}`,
  type: "Person",
  _label: "Douris",
  identified_by: [name("Douris", ENGLISH)],
  born: {
    type: "Birth",
    timespan: {
      type: "TimeSpan",
      begin_of_the_begin: "-0509-01-01T00:00:00Z",
      end_of_the_end: "-0499-12-31T23:59:59Z",
    },
  },
};

const { x, y, width, height } = IMAGE_REGION;

const IMAGE = {
  "@context": CONTEXT,
  id: `${READER_BASE_URI}${IMAGE_ID}`,
  type: "HumanMadeObject",
  _label: "Kylix, detail",
  identified_by: [name("Kylix, detail", ENGLISH)],
  representation: [
    {
      type: "VisualItem",
      digitally_shown_by: [
        {
          type: "DigitalObject",
          access_point: [
            {
              id: `${IMAGE_SERVICE}/${[x, y, width, height].join(",")}/max/0/default.jpg`,
              type: "DigitalObject",
            },
          ],
        },
      ],
    },
  ],
};

// Where Linked Art puts a IIIF Manifest: `subject_of` → `digitally_carried_by`.
const WITH_MANIFEST = {
  "@context": CONTEXT,
  id: `${READER_BASE_URI}${MANIFEST_ID}`,
  type: "HumanMadeObject",
  _label: "Kylix, manifest",
  identified_by: [name("Kylix, manifest", ENGLISH)],
  subject_of: [
    {
      type: "LinguisticObject",
      digitally_carried_by: [
        {
          type: "DigitalObject",
          format: 'application/ld+json;profile="http://iiif.io/api/presentation/3/context.json"',
          conforms_to: [{ id: "http://iiif.io/api/presentation/", type: "InformationObject" }],
          access_point: [{ id: MANIFEST, type: "DigitalObject" }],
        },
      ],
    },
  ],
};

export const READER_RECORDS = [KYLIX, PAINTER, IMAGE, WITH_MANIFEST];
