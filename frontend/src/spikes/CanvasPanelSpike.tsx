/**
 * Spike S5 (Phase 0, Step 0.10): Canvas Panel web components inside React 19.
 *
 * A developer page, not routed from the app shell. Served by Vite at
 * `/src/spikes/canvas-panel.html` and driven by `CanvasPanelSpike.spec.ts`.
 * Results: `docs/spikes/S5-canvas-panel.md`.
 */
import "@digirati/canvas-panel-web-components";

import { useCallback, useMemo, useRef, useState, type CSSProperties } from "react";

import type {
  CanvasPanelElement,
  ContentStateSelectionEvent,
  SequencePanelElement,
} from "./canvasPanelElements.ts";
import {
  API_REGION,
  AUDIO,
  BOOK,
  CONTENT_STATE_REGION,
  IMAGE,
  IMAGE_REGION,
  VIDEO,
  canvasRegionContentState,
  encodeContentState,
  toXywh,
} from "./fixtures.ts";

// Canvas Panel's Atlas viewer ignores the element's CSS height (it falls back to 512px), so every
// panel also gets a `height` attribute; `overflow: hidden` clips the absolutely positioned A/V overlay.
const panelStyle: CSSProperties = {
  display: "block",
  position: "relative",
  overflow: "hidden",
  blockSize: "360px",
  inlineSize: "640px",
};

/** A React 19 prop React cannot map: camelCase names become lower-cased, unobserved attributes. */
const camelCaseProps: object = { manifestId: IMAGE.manifest, canvasId: IMAGE.canvas };

function describeMedia(element: HTMLMediaElement | null | undefined): string {
  if (!element) {
    return "no-element";
  }
  return JSON.stringify({
    src: element.currentSrc || element.getAttribute("src"),
    readyState: element.readyState,
    currentTime: Math.round(element.currentTime * 10) / 10,
    paused: element.paused,
    error: element.error?.code ?? null,
  });
}

export function CanvasPanelSpike() {
  const imageRef = useRef<CanvasPanelElement>(null);
  const sequenceRef = useRef<SequencePanelElement>(null);
  const videoRef = useRef<CanvasPanelElement>(null);
  const audioRef = useRef<CanvasPanelElement>(null);

  const [imageReady, setImageReady] = useState(false);
  const [imageCanvas, setImageCanvas] = useState<string>();
  const [region, setRegion] = useState<string>();
  const [position, setPosition] = useState<string>();
  const [selection, setSelection] = useState<ContentStateSelectionEvent>();
  const [contentStateCanvas, setContentStateCanvas] = useState<string>();
  const [contentStateReady, setContentStateReady] = useState(false);
  const [sequencePosition, setSequencePosition] = useState<string>();
  const [videoState, setVideoState] = useState<string>();
  const [audioState, setAudioState] = useState<string>();

  const contentState = useMemo(
    () =>
      encodeContentState(
        JSON.stringify(
          canvasRegionContentState(
            BOOK.manifest,
            BOOK.canvases[1],
            `xywh=${toXywh(CONTENT_STATE_REGION)}`,
          ),
        ),
      ),
    [],
  );

  const readPosition = useCallback(() => {
    setPosition(JSON.stringify(imageRef.current?.getPosition?.() ?? null));
  }, []);

  const startSelection = useCallback(() => {
    imageRef.current?.enableContentStateSelection?.((event) => setSelection(event));
  }, []);

  const stopSelection = useCallback(() => {
    imageRef.current?.disableContentStateSelection?.();
  }, []);

  const goToRegion = useCallback(() => {
    imageRef.current?.goToTarget?.(API_REGION, { immediate: true });
  }, []);

  const play = useCallback((ref: typeof videoRef, update: (state: string) => void) => {
    const media = ref.current?.mediaElement?.current;
    if (media) {
      media.muted = true;
      media.addEventListener("timeupdate", () => update(describeMedia(media)));
      media.addEventListener("error", () => update(describeMedia(media)));
    }
    ref.current?.mediaActions?.play();
    update(describeMedia(media));
  }, []);

  return (
    <main data-testid="spike-s5" style={{ padding: "1rem", display: "grid", gap: "2rem" }}>
      <h1>Spike S5: Canvas Panel in React 19</h1>

      <section aria-labelledby="s5-image">
        <h2 id="s5-image">Image Canvas by manifest and canvas id</h2>
        <canvas-panel
          ref={imageRef}
          data-testid="cp-image"
          tabIndex={0}
          style={panelStyle}
          height="360"
          manifest-id={IMAGE.manifest}
          canvas-id={IMAGE.canvas}
          region={region}
          move-events="true"
          a11y-title="Picture of Göttingen"
          onworld-ready={() => setImageReady(true)}
          oncanvas-change={(event) => setImageCanvas(event.detail.canvas)}
          onmove={readPosition}
          onzoom={readPosition}
        />
        <p>
          <button type="button" onClick={() => setRegion(toXywh(IMAGE_REGION))}>
            Set region attribute
          </button>{" "}
          <button type="button" onClick={goToRegion}>
            Go to region (API)
          </button>{" "}
          <button type="button" onClick={startSelection}>
            Draw a region
          </button>{" "}
          <button type="button" onClick={stopSelection}>
            Stop drawing
          </button>
        </p>
        <dl>
          <dt>world-ready</dt>
          <dd>
            <output data-testid="image-ready">{String(imageReady)}</output>
          </dd>
          <dt>canvas-change</dt>
          <dd>
            <output data-testid="image-canvas">{imageCanvas}</output>
          </dd>
          <dt>Viewport (canvas coordinates)</dt>
          <dd>
            <output data-testid="image-position">{position}</output>
          </dd>
          <dt>Selection as Content State target</dt>
          <dd>
            <output data-testid="selection-id">{selection?.contentState.id}</output>
          </dd>
          <dt>Selection as encoded Content State</dt>
          <dd>
            <output data-testid="selection-encoded">{selection?.encodedContentState}</output>
          </dd>
        </dl>
      </section>

      <section aria-labelledby="s5-content-state">
        <h2 id="s5-content-state">IIIF Content State (iiif-content)</h2>
        <canvas-panel
          data-testid="cp-content-state"
          style={panelStyle}
          height="360"
          iiif-content={contentState}
          onworld-ready={() => setContentStateReady(true)}
          oncanvas-change={(event) => setContentStateCanvas(event.detail.canvas)}
        />
        <dl>
          <dt>world-ready</dt>
          <dd>
            <output data-testid="content-state-ready">{String(contentStateReady)}</output>
          </dd>
          <dt>canvas-change</dt>
          <dd>
            <output data-testid="content-state-canvas">{contentStateCanvas}</output>
          </dd>
          <dt>iiif-content</dt>
          <dd>
            <output data-testid="content-state-value">{contentState}</output>
          </dd>
        </dl>
      </section>

      <section aria-labelledby="s5-sequence">
        <h2 id="s5-sequence">sequence-panel navigation</h2>
        <sequence-panel
          ref={sequenceRef}
          data-testid="sequence"
          style={panelStyle}
          height="360"
          manifest-id={BOOK.manifest}
          onsequence-change={(event) =>
            setSequencePosition(`${event.detail.index + 1} / ${event.detail.total}`)
          }
        />
        <p>
          <button type="button" onClick={() => sequenceRef.current?.sequence?.previousCanvas()}>
            Previous
          </button>{" "}
          <button type="button" onClick={() => sequenceRef.current?.sequence?.nextCanvas()}>
            Next
          </button>{" "}
          <output data-testid="sequence-position">{sequencePosition}</output>
        </p>
      </section>

      <section aria-labelledby="s5-av">
        <h2 id="s5-av">Audio and video in Canvas Panel</h2>
        <canvas-panel
          ref={videoRef}
          data-testid="cp-video"
          style={panelStyle}
          height="360"
          manifest-id={VIDEO.manifest}
          canvas-id={VIDEO.canvas}
          onmedia-displayed={() =>
            setVideoState(describeMedia(videoRef.current?.mediaElement?.current))
          }
        />
        <p>
          <button type="button" onClick={() => play(videoRef, setVideoState)}>
            Play video
          </button>{" "}
          <button type="button" onClick={() => videoRef.current?.mediaActions?.pause()}>
            Pause video
          </button>{" "}
          <output data-testid="video-state">{videoState}</output>
        </p>
        <canvas-panel
          ref={audioRef}
          data-testid="cp-audio"
          style={{ ...panelStyle, blockSize: "120px" }}
          height="120"
          manifest-id={AUDIO.manifest}
          canvas-id={AUDIO.canvas}
          onmedia-displayed={() =>
            setAudioState(describeMedia(audioRef.current?.mediaElement?.current))
          }
        />
        <p>
          <button type="button" onClick={() => play(audioRef, setAudioState)}>
            Play audio
          </button>{" "}
          <button type="button" onClick={() => audioRef.current?.mediaActions?.pause()}>
            Pause audio
          </button>{" "}
          <output data-testid="audio-state">{audioState}</output>
        </p>
      </section>

      <section aria-labelledby="s5-pitfall">
        <h2 id="s5-pitfall">Pitfall: camelCase props are not observed</h2>
        <canvas-panel
          data-testid="cp-camelcase"
          style={{ ...panelStyle, blockSize: "40px" }}
          {...camelCaseProps}
        />
      </section>
    </main>
  );
}
