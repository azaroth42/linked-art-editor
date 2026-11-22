# Questions for humans

Questions raised while implementing guide steps. Each section names the step; answer inline or in
the step's follow-up issue.

## Step 0.4

1. **Institutional records are not from `lux.collections.yale.edu`.** Every plain `GET` to LUX
   (`/data/...` and `/api/search/...`) returns `202` with an empty body and
   `x-amzn-waf-action: challenge` (an AWS WAF JavaScript challenge), and the adjacent repositories'
   test fixtures hold only synthetic records. The 33 records in `tests/corpus/institutional/` come
   instead from the public Linked Art feeds LUX harvests (YCBA, YUAG, Yale University Library);
   provenance per file is in `tests/corpus/SOURCE.md`. Not verified: (a) whether you want merged
   LUX records as well (a sample export from the domain lead would do; drop the files into
   `tests/corpus/institutional/` and add rows to `SOURCE.md`), and (b) the licence for committing
   these records to the repository; the records themselves state no metadata licence.
2. **The vendored JSON Schema includes uncommitted upstream changes.** The local
   `linked-art/json-validator` checkout had uncommitted edits to `abstract.json`, `core.json` and
   `linked_art.json` (fixing duplicate `$id`s and adding properties). They were vendored as found,
   so `profiles/schema/` matches no upstream commit; `profiles/schema/SOURCE.md` lists the changes
   and SHA-256 hashes. Please commit them upstream so the pin is reproducible. The same file also
   records an upstream defect: `provenance.json` refers to `core.json#/$defs/LegalRight`, which
   does not exist.
3. **60 of 244 corpus files fail the schema**, mostly drift between the Dec 2024 schema and the
   Nov 2026 examples (references without `id`, top-level `_label` missing, array versus object
   cardinality). They are listed in `tests/corpus/KNOWN_FAILURES.md`. Decide whether the schema or
   the examples should change upstream; Step 1.9 makes JSON Schema the publish gate, so as things
   stand these records would be blocked from publishing.
