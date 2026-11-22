# Spike S5: Canvas Panel in React 19

**Result: GO**, with the conditions in [section 8](#8-go--no-go). Canvas Panel 1.0.74 works inside React 19
for every capability the step asks about. Four defects and two missing features were measured; each one
has a host-side workaround, and the editor has to build A/V controls and temporal fragments itself.
Canvas Panel is the only viewer (ADR-0012), so there is no fallback to evaluate.

| | |
| :-- | :-- |
| Step | Phase 0, Step 0.10 (issue #10) |
| Date | 2026-11-22 |
| Machine | macOS (Darwin 25.6, arm64), Node 26, pnpm 12.21.0 via Corepack |
| Frontend | React 19.3.1, TypeScript 6.0.3, Vite 8.3.8 |
| Browsers | Playwright 1.65.1 headless Chromium (headless shell build 1255); Google Chrome (branded), headless, for a cross-check |
| Tests | `frontend/src/spikes/CanvasPanelSpike.spec.ts` |

## 1. Package

| Package | Version | Notes |
| :-- | :-- | :-- |
| `@digirati/canvas-panel-web-components` | **1.0.74** (exact pin) | The current name on npm. Latest release, published 2025-06-10; nothing newer exists. MIT. |

`<sequence-panel>` is not a separate package. The published entry point (`dist/bundle.js`) is one
self-contained IIFE, with Preact 10 bundled inside, that registers `<canvas-panel>`, `<sequence-panel>`,
`<image-service>`, `<atlas-viewer>`, `<layout-container>`, `<metadata-panel>` and `<range-panel>`.
A side-effect import is all the app needs: `import "@digirati/canvas-panel-web-components";`.

The package also ships `dist/esm/lib.react.mjs`, a React build of the components. **Do not use it**: it
imports `render` from `react-dom`, which React 19 removed. Use the web components.

## 2. Public IIIF resources used

All from the IIIF Cookbook, served with `Access-Control-Allow-Origin: *`. They are listed in
`frontend/src/spikes/fixtures.ts`.

| Case | Manifest | Canvas(es) | Content |
| :-- | :-- | :-- | :-- |
| Image | https://iiif.io/api/cookbook/recipe/0005-image-service/manifest.json | `…/canvas/p1` | 4032 × 3024, IIIF Image API 3 service (level 1, 512 px tiles) |
| Multi-canvas | https://iiif.io/api/cookbook/recipe/0009-book-1/manifest.json | `…/canvas/p1` … `p5` | 5 pages with image services, `behavior: paged` |
| Video | https://iiif.io/api/cookbook/recipe/0003-mvm-video/manifest.json | `…/canvas` | 480 × 360, 572 s, MP4 (H.264) from `fixtures.iiif.io` |
| Sound | https://iiif.io/api/cookbook/recipe/0002-mvm-audio/manifest.json | `…/canvas` | 1985 s, MP4 (AAC) from `fixtures.iiif.io` |
| Content State | built in `fixtures.ts`: an `Annotation` with `motivation: contentState` targeting book `p2#xywh=400,600,1600,1200`, `partOf` the book Manifest, encoded per Content State API 1.0 §6 (percent-encode, then base64url without padding) | | |

## 3. How it was tested

`frontend/src/spikes/canvas-panel.html` loads `CanvasPanelSpike.tsx` under `<StrictMode>`. The page is
not routed from the app shell. Playwright starts Vite on slot 10's port (9010) with
`src/spikes/vite.config.ts` and drives the page:

```
cd frontend
corepack pnpm test:spike-s5                          # bundled headless Chromium
SPIKE_S5_CHROME=1 corepack pnpm test:spike-s5        # also branded Chrome
corepack pnpm exec vite build --config src/spikes/vite.config.ts   # bundle sizes
```

The tests need the public network, so they are not part of the app's E2E suite or CI. Tests wrapped in
`test.fail()` record measured gaps: each asserts what the editor wants and is expected to fail against
1.0.74. If one starts passing after an upgrade, Playwright reports it.

**Measured:** Chromium 14 / 14 (10 capability tests, 4 expected-fail gap tests); Chromium and Chrome
together 28 / 28; five repeats on Chromium (`--repeat-each 5`) 70 / 70. An earlier repeat run was flaky
in the selection test: sketch mode only listens for the mouse after a Preact effect runs, so a drag
that arrived first panned the image instead of drawing. The test now waits for the effect and for a
settled viewport. The editor needs the same wait before it starts a programmatic drag.

## 4. Capabilities

| # | Capability | Result | Evidence and measurements |
| :-- | :-- | :-- | :-- |
| 1 | Load by manifest id + canvas id | **Pass** | `world-ready` and `canvas-change` reach React (`canvas-change` reports the canvas id); tiles are fetched from the image service and drawn on one `<canvas>` in the shadow root; the home view frames the whole 4032 × 3024 Canvas. |
| 2 | Set a region programmatically | **Pass** | `region="1000,800,1200,900"` (React 19 sets the element property, which reflects to the attribute): the view becomes x 800, y 800, w 1600, h 900, the region widened to the element's 16:9; `getTarget()` returns the `BoxSelector`. `goToTarget({x: 2400, y: 1200, width: 800, height: 450})` frames that region too. Setting `region` also makes it the home position (`goHome()` returns to it). |
| 3 | Listen for a selection and read it back as `xywh` | **Pass** | `enableContentStateSelection(callback)` switches to sketch mode. With the view at x 800, y 800, w 1600, h 900, a mouse drag from 25 %/25 % to 60 %/70 % of the panel produced `…/canvas/p1#xywh=1200,1023,560,405`, against 1200,1025,560,405 computed from the viewport geometry; the test allows 2 screen px. The callback also receives the selection as an encoded Content State (`{id, type: Canvas, partOf: [Manifest]}`), which decodes to the same fragment. It is a callback, not a DOM event. Drawing is mouse-only (see 6). |
| 4 | Load an `iiif-content` Content State | **Partial** | The Canvas (`p2`) and Manifest named by the Content State load. Its `xywh` is **ignored on first load**: the view frames the whole page (x −2506, w 8198). Workaround, tested: after `world-ready`, `setContentStateFromText(value, true)` frames the region (x 133, y 600, w 2133, h 1200 for region 400,600,1600,1200). |
| 5 | `<sequence-panel>` navigation | **Pass** | The paged book loads as 3 views, `[[0], [1, 2], [3, 4]]` (cover, then spreads); `element.sequence.nextCanvas()` / `previousCanvas()` move 1/3 → 2/3 → 3/3 → 2/3, reported by the `sequence-change` event (`{index, total}`). There are no built-in buttons; the host draws them. |
| 6 | A/V in Canvas Panel | **Pass for playback; gaps** | Both Canvases render a `<video>` in the shadow root with the Manifest's MP4 as `src`. `element.mediaActions.play()` (muted) advanced `currentTime` past 0.5 s with no `MediaError`, for the video (duration 572.03 s) and the sound (1985 s), in bundled headless Chromium and in Chrome. Gaps: no transport controls (the `<video>` has no `controls`; a click on the video toggles play/pause, and the sound has no visible UI at all, just an empty 300 × 150 box); a temporal Content State (`#t=30,60`) does not seek (`currentTime` stays 0); `media-displayed` fires before `mediaElement.current` exists. |
| 7 | Keyboard | **Pass** | With `tabIndex={0}` on the element (the host must add it), `=` / `-` / `0` zoom and reset and the arrow keys pan. |
| 8 | Content State out (current view) | **Pass, values need cleaning** | After keyboard navigation, `getContentState().contentState.id` was `…/canvas/p1#xywh=823.1185302734375,755.73876953125,2687.9283447265625,1512.5224609375`: the raw viewport, fractional, and negative at the home position. Media Fragments need non-negative integers, so the editor must round and clamp before storing. |

### Measured gaps (the `test.fail()` tests)

1. `iiif-content` ignores the Content State's `xywh` on first load (capability 4).
2. `getCanvasId()` / `getManifestId()` return `undefined` when the value came from the initial
   attribute. Canvas Panel's `useSyncedState` fills its ref only when the value *changes*. Use the
   `canvas-change` event, or the host's own props.
3. `getContentState()` exports fractional and negative `xywh` values (capability 8).
4. A temporal Content State (`#t=`) is ignored: Canvas Panel handles only `BoxSelector` targets.

Untested-by-design gaps found in the source: region drawing listens to `mousedown`/`mousemove`/`mouseup`
on the inner `<canvas>` only, so there is no keyboard or touch path; and there are no A/V controls
(capability 6).

## 5. React 19 custom-element findings

1. **Kebab-case attributes only.** React 19 assigns a prop as a DOM property when `propName in element`,
   otherwise it sets an attribute. Canvas Panel defines its properties under the attribute names
   (`"manifest-id"`), so `manifest-id={…}` works and `manifestId={…}` becomes the attribute `manifestid`,
   which nothing observes. The panel stays empty. Tested.
2. **Define before render.** The property path needs the element upgraded when React creates it, so import
   the bundle before rendering (statically, or `await customElements.whenDefined("canvas-panel")` after a
   dynamic import). Strings still work on an un-upgraded element (React falls back to `setAttribute`), but
   objects (a `Selector` for `region`, a shared `vault`) would become `"[object Object]"`.
3. **Booleans as strings.** Canvas Panel parses booleans with `value.toLowerCase() === "true"`. A bare
   attribute (`""`) is false. Pass `"true"` / `"false"`, as the typings in `canvasPanelElements.ts` require.
4. **Events.** React 19 binds `on<event>` props on custom elements with `addEventListener("<event>")`. The
   names are case-sensitive and kebab-case: `onworld-ready`, `oncanvas-change`, `onsequence-change`,
   `onmedia-displayed`, `onmove`, `onzoom`. The tests verify the first four through the page's outputs. No
   `ref` + `useEffect` listener plumbing is needed.
5. **Methods appear late.** `getPosition`, `goToTarget`, `enableContentStateSelection` and the rest are
   assigned to the instance when it connects and renders, not to the prototype. `element.sequence` appears
   once the Manifest has loaded, and `element.mediaActions` once a Sound or Video Canvas renders. They are
   typed as optional and called with `?.()`.
6. **Typing.** The package's typings describe Preact component props, not DOM elements, so
   `canvasPanelElements.ts` declares `JSX.IntrinsicElements` for `canvas-panel` and `sequence-panel` by
   augmenting `declare module "react" { namespace JSX { … } }` (one `eslint-disable` for
   `@typescript-eslint/no-namespace`). Event props are typed `CustomEvent<Detail>`.
7. **Sizing.** The Atlas viewer ignores the element's CSS height and falls back to 512 px unless the
   `height` attribute is set, and the A/V overlay is absolutely positioned and escapes the element. Set
   both the `height` attribute and `overflow: hidden`.
8. **No runtime conflict.** Preact lives inside the IIFE; React 19 and Canvas Panel do not share anything.
   `StrictMode` caused no problems: `enableContentStateSelection` ignores a second registration.

## 6. Bundle size and install footprint

| Build | Output | Raw | gzip |
| :-- | :-- | --: | --: |
| `vite build`, before (app only) | `index-*.js` | 219.61 kB | 68.62 kB |
| `vite build`, after adding the dependency (app does not import it) | `index-*.js` | 219.61 kB | 68.62 kB |
| `vite build --config src/spikes/vite.config.ts` (app + spike page) | shared React chunk `jsx-runtime-*.js` | 219.40 kB | 68.53 kB |
| | spike page chunk `spike-s5-*.js` | **594.18 kB** | **174.71 kB** |
| Same two builds after rebasing onto Step 0.2 (i18n in the app) | `index-*.js` (app only) | 318.73 kB | 100.03 kB |
| | app + spike: `app-*.js` / `jsx-runtime-*.js` / `spike-s5-*.js` | 98.72 / 219.90 / **594.20 kB** | 31.62 / 68.77 / **174.71 kB** |

Nearly all of the spike chunk is Canvas Panel. The published `bundle.js` alone is 600,643 B raw,
179,330 B with `gzip -9`, and 140,144 B with `brotli -q 11`; the built spike chunk compresses to
172,361 B gzip -9 and 134,146 B brotli. The IIFE cannot be tree-shaken or split, and Vite warns about a
chunk over 500 kB. So wherever Canvas Panel is imported it costs about **175 kB gzip (135 kB brotli)**,
roughly 2.5 times React and ReactDOM together.

Install footprint: the package declares its build-time dependencies as runtime dependencies, so
`pnpm add` brought **49 packages** into the lockfile, among them a second `react@18.3.1` and
`react-dom@18.3.1`, `react-i18next@11.18.6`, `typescript@4.9.5` and `preact@10.29.8`. None of them reach
the bundle. pnpm reports two unmet peers inside that tree: `react-reconciler@0.29.2` wants React 18, and
`@iiif/helpers@1.6.1` wants `@iiif/parser ^2.2.8`. The package itself is 4.3 MB in `node_modules`.

## 7. Risks and what was not verified

- **Maintenance.** 1.0.74 is about 17 months old, on `@iiif/vault` 0.9 and `@atlas-viewer/atlas` 2.0.17.
  Fixes for the gaps above are ours to carry: wrapper workarounds, a `pnpm patch`, or upstream
  contributions.
- **Not shown headlessly:** that sound is audible and that the drawn pixels look right. The tests assert
  that `currentTime` advances without a `MediaError`, that tiles are fetched, and that a shadow `<canvas>`
  exists. Rendering was checked once by eye from a full-page screenshot, not by an assertion.
- **Not verified on Linux.** Every run was on macOS, where Playwright's bundled Chromium decodes
  H.264/AAC (`canPlayType` returned `probably`). Playwright's Linux Chromium build may lack those
  decoders. The A/V tests skip with a message when `canPlayType` is empty; rerun them with
  `SPIKE_S5_CHROME=1` there.
- **Not verified: touch or pen region drawing.** The source listens for mouse events only, so a touch
  drag most likely pans instead of drawing.
- **Network.** The tests depend on `iiif.io` and `fixtures.iiif.io` being up.

## 8. GO / NO-GO

**GO.** Canvas Panel 1.0.74 embeds in React 19 and covers image viewing by Manifest + Canvas,
programmatic regions, region selection as `xywh` and as Content State, Content State input,
`sequence-panel` navigation, and A/V playback. These conditions apply to Phase 1 Step 1.21 (Public
Reader) and Phase 2 Step 2.12 (`IIIFViewer` widget):

1. Wrap Canvas Panel in one component (`frontend/src/widgets/IIIFViewer.tsx`) that owns the rules in
   section 5: kebab-case attributes, string booleans, the `height` attribute, `overflow: hidden`,
   `on<event>` props, and optional-chained methods. Start from `src/spikes/canvasPanelElements.ts`.
2. After `world-ready`, apply a Content State's region with `setContentStateFromText(value, true)`.
3. Track the current Canvas from `canvas-change`, not `getCanvasId()`.
4. Round and clamp every `xywh` from Canvas Panel to non-negative integers inside the Canvas before
   writing a `SpecificResource` fragment.
5. Region selection needs a keyboard and single-pointer equivalent (conventions §5, WCAG 2.2 SC 2.5.7):
   numeric x/y/w/h fields, plus "use current view" (keyboard pan and zoom, then `getContentState()`).
6. A/V: build transport controls (play/pause, seek, time, mute) with translated labels on
   `element.mediaActions` and `element.mediaElement`; implement temporal fragments (`#t=`) in the
   wrapper (parse, then `mediaActions.setTime`); wait for `mediaElement.current`, not for
   `media-displayed`.
7. Load the bundle lazily, with a dynamic `import()` on the routes that show media, then
   `customElements.whenDefined`. The server-rendered Public Reader can serve `dist/bundle.js` as a
   static `<script defer>` with no React involved.

The gaps are listed for the owner in `docs/questions.md` under "Step 0.10".

## 9. Files

| File | Purpose |
| :-- | :-- |
| `frontend/src/spikes/CanvasPanelSpike.tsx` | The spike page (not routed from the app shell) |
| `frontend/src/spikes/canvasPanelElements.ts` | Element and `JSX.IntrinsicElements` typings for React 19 |
| `frontend/src/spikes/fixtures.ts` | Public IIIF URLs, regions, Content State encoding |
| `frontend/src/spikes/CanvasPanelSpike.spec.ts` | Playwright proofs and gap tests |
| `frontend/src/spikes/canvas-panel.html`, `main.tsx` | Page entry |
| `frontend/src/spikes/vite.config.ts`, `playwright.config.ts` | Dev server and build for the spike; test runner |
| `frontend/eslint.config.js` | `i18next/no-literal-string` off for `src/spikes/**/*.tsx` only (developer harness, not shipped), with a comment; spike configs and spec get Node globals |
| `frontend/tsconfig.json`, `tsconfig.node.json` | Spike configs and spec are type-checked as Node code, like `e2e/` |
