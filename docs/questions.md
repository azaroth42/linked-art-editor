# Questions for humans

Questions raised while implementing guide steps. Each section names the step; answer inline or in
the step's follow-up issue.

## Step 0.3

1. **Not verified: Linux and systemd.** `deploy/systemd/*.service` and the Debian/Ubuntu install
   steps in `deploy/README.md` were written but not run: the development machine is macOS, with
   no Linux host or systemd. What was verified on macOS: `make up` / `make run` / `make down`, and
   that `qlever start --run-in-foreground` (what `qlever.service` runs) shuts the server down on
   `SIGINT` (`KillSignal=SIGINT`). Please run the production steps once on a Debian/Ubuntu host.
2. **QLever cannot bind to localhost only.** The guide says QLever binds only to localhost / the
   internal network, but `qlever-server` listens on `0.0.0.0`, hard-coded in
   `src/engine/Server.cpp` (also in the current `lux/qlever-dev` master); there is no option. The
   port has to be closed with a host firewall; `deploy/README.md` and `qlever.service` say so.
   Decide whether that is acceptable or whether an upstream bind option should be requested.
3. **Verified binary is older than the reference source.** Homebrew `qlever` 0.6.0_1 is built
   from git `6c5861a` (2026-08-13), 480 commits behind the `lux/qlever-dev` checkout `ba7b74c`
   (2026-11-21). Both are recorded in `deploy/QLEVER_VERSION`. Decide whether Spike S1 must use a
   source build at the newer commit.

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
