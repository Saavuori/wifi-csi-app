// Phase 3: the scrolling spectrogram.
//
// Subcarrier index on Y, time on X, amplitude as colour. This is the most informative view in
// the app — you see someone walk past as a vertical smear — and it is also the thing to check
// first when something is wrong, because no downstream DSP rescues a bad signal.
//
// It is not only a visualization. The published ESP32 activity-recognition work feeds CSI
// amplitude spectrograms straight into CNNs, so this view and a future model input are the same
// artifact. Hence: build it once, properly.
//
// Rendering strategy, in the order the plan puts them:
//
//   1. Never render at the data rate. Frames arrive batched from the worker; drawing happens on
//      requestAnimationFrame, and each repaint blits however many columns accumulated. The
//      display is 60 Hz and the data is 80-100 Hz, so drawing per frame is wasted work that
//      degrades into stutter over a long session.
//   2. Scroll by drawing the canvas onto itself, offset by N pixels, rather than repainting
//      history. Cost per repaint is then a function of how many *new* columns there are, not of
//      how much history is on screen.
//   3. Binary over the WebSocket, decoded off-thread. Done in workers/socket.ts.
//
// The remaining step from the plan is moving the drawing itself into the worker with
// OffscreenCanvas. Steps 1-3 remove essentially all of the pain; that one is the follow-up.

import { colormap, colormapGradient, type ColormapName } from "../lib/colormap";
import { el, fitCanvas, select, slider, toggle } from "../lib/dom";
import type { FrameBatch, Metrics } from "../lib/messages";
import { store } from "../lib/store";
import type { View } from "./view";

// Bounds for the display normalization. `z` here is per-subcarrier standard deviations from
// that subcarrier's running mean — see the note on `normalize` below.
const DEFAULT_RANGE = 2.5;

interface Options {
  colormap: ColormapName;
  range: number;
  normalize: boolean;
  showSelected: boolean;
  showAgc: boolean;
}

export function waterfallView(): View {
  const options: Options = {
    colormap: "viridis",
    range: DEFAULT_RANGE,
    normalize: true,
    showSelected: true,
    showAgc: true,
  };

  const canvas = el("canvas", { class: "waterfall-canvas" });
  const overlay = el("canvas", { class: "waterfall-overlay" });
  const status = el("div", { class: "waterfall-status" });

  const context = canvas.getContext("2d", { alpha: false });
  const overlayContext = overlay.getContext("2d");

  // Per-subcarrier running mean and variance, for display contrast. Amplitudes differ by an
  // order of magnitude across the band — a subcarrier in a fade is simply darker than one at a
  // peak — and a single global scale wastes almost the whole colour range showing that static
  // difference instead of the changes we care about. Normalizing per subcarrier spends the
  // colour range on deviation from each carrier's own recent behaviour.
  let mean: Float32Array | null = null;
  let variance: Float32Array | null = null;
  let nSub = 0;

  // Columns waiting to be drawn on the next animation frame.
  let queue: { amp: Float32Array; agc: boolean }[] = [];
  let scratch: ImageData | null = null;
  let running = true;
  let framesDrawn = 0;
  let lastAgcAt = 0;
  let selected = new Set<number>();

  // EMA coefficient for the running statistics. ~4 s at 80 Hz: long enough to be a stable
  // reference, short enough to follow the slow channel drift the plan warns about over hours.
  const ALPHA = 0.003;

  function resetStats(count: number) {
    nSub = count;
    mean = new Float32Array(count);
    variance = new Float32Array(count).fill(1);
    // Seeded on the first column below; until then the display is flat rather than wrong.
  }

  function updateStats(amp: Float32Array, seeded: boolean) {
    if (mean === null || variance === null) return;
    for (let i = 0; i < nSub; i++) {
      const value = amp[i];
      if (!Number.isFinite(value)) continue;
      if (!seeded) {
        mean[i] = value;
        variance[i] = 1;
        continue;
      }
      const delta = value - mean[i];
      mean[i] += ALPHA * delta;
      variance[i] += ALPHA * (delta * delta - variance[i]);
    }
  }

  function drawColumns() {
    if (context === null || queue.length === 0 || mean === null || variance === null) {
      return;
    }

    const width = canvas.width;
    const height = canvas.height;
    const columns = Math.min(queue.length, width);
    const batch = queue.slice(queue.length - columns);
    queue = [];

    // Scroll the existing image left by `columns` pixels by drawing the canvas onto itself.
    // This is the one trick that keeps a long session cheap: history is never re-rendered.
    context.globalCompositeOperation = "copy";
    context.drawImage(canvas, -columns, 0);
    context.globalCompositeOperation = "source-over";

    if (scratch === null || scratch.width !== columns || scratch.height !== height) {
      scratch = context.createImageData(columns, height);
    }

    const lut = colormap(options.colormap);
    const pixels = scratch.data;
    const scale = height / nSub;

    for (let c = 0; c < columns; c++) {
      const { amp, agc } = batch[c];
      for (let y = 0; y < height; y++) {
        // Flip so low subcarrier indices sit at the bottom, matching the axis labels.
        const sub = Math.min(nSub - 1, Math.floor((height - 1 - y) / scale));
        const value = amp[sub];
        const offset = (y * columns + c) * 4;

        if (!Number.isFinite(value)) {
          // A masked subcarrier — guard band, DC, or a pilot. Drawn as a gap rather than
          // closed up, so the Y axis keeps meaning the same thing when the mask changes.
          pixels[offset] = 26;
          pixels[offset + 1] = 28;
          pixels[offset + 2] = 34;
          pixels[offset + 3] = 255;
          continue;
        }

        let t: number;
        if (options.normalize) {
          const sigma = Math.sqrt(Math.max(variance[sub], 1e-6));
          t = (value - mean[sub]) / (sigma * options.range) / 2 + 0.5;
        } else {
          t = value / (80 * options.range);
        }

        const index = Math.max(0, Math.min(255, Math.round(t * 255))) * 3;
        pixels[offset] = lut[index];
        pixels[offset + 1] = lut[index + 1];
        pixels[offset + 2] = lut[index + 2];
        pixels[offset + 3] = 255;

        if (agc && options.showAgc && (y & 3) === 0) {
          // Mark the gain event without hiding the data under it: a dashed red tint on every
          // fourth row. These columns are excluded from variance accumulation server-side, and
          // seeing where they fall is how you check that the AGC handling is working rather
          // than trusting that it is.
          pixels[offset] = 220;
          pixels[offset + 1] = 70;
          pixels[offset + 2] = 70;
        }
      }
    }

    context.putImageData(scratch, width - columns, 0);
    framesDrawn += columns;
  }

  function drawOverlay() {
    if (overlayContext === null) return;
    const width = overlay.width;
    const height = overlay.height;
    overlayContext.clearRect(0, 0, width, height);
    if (nSub === 0) return;

    const ratio = window.devicePixelRatio || 1;
    overlayContext.font = `${11 * ratio}px ui-monospace, monospace`;
    overlayContext.textBaseline = "middle";

    // Subcarrier axis. Ticks every 8 indices is dense enough to locate a feature and sparse
    // enough to stay readable at any panel height.
    overlayContext.strokeStyle = "rgba(255,255,255,0.08)";
    overlayContext.fillStyle = "rgba(255,255,255,0.45)";
    for (let sub = 0; sub < nSub; sub += 8) {
      const y = height - 1 - (sub / nSub) * height;
      overlayContext.beginPath();
      overlayContext.moveTo(0, y);
      overlayContext.lineTo(width, y);
      overlayContext.stroke();
      overlayContext.fillText(String(sub), 4 * ratio, y - 8 * ratio);
    }

    if (!options.showSelected || selected.size === 0) return;

    // Which subcarriers the analysis is currently using. Showing them here is the point of the
    // plan's "sanity-check the choice visually": a ranking that has landed on a band of dead
    // carriers is obvious on the waterfall and invisible in a list of numbers.
    overlayContext.strokeStyle = "rgba(255, 209, 102, 0.85)";
    overlayContext.lineWidth = Math.max(1, ratio);
    for (const sub of selected) {
      const y = height - 1 - ((sub + 0.5) / nSub) * height;
      overlayContext.beginPath();
      overlayContext.moveTo(width - 34 * ratio, y);
      overlayContext.lineTo(width, y);
      overlayContext.stroke();
    }
  }

  function onFrames(batch: FrameBatch) {
    if (!running) return;
    if (batch.nSub !== nSub) resetStats(batch.nSub);

    for (let i = 0; i < batch.count; i++) {
      const amp = batch.amp.subarray(i * batch.nSub, (i + 1) * batch.nSub);
      updateStats(amp, framesDrawn + queue.length > 0);
      // The subarray is a view onto the transferred buffer, which lives as long as the queue
      // entry does, so no copy is needed here.
      queue.push({ amp, agc: batch.agc[i] === 1 });
      if (batch.agc[i] === 1) lastAgcAt = performance.now();
    }

    // Cap the queue at one screen width. If the tab was backgrounded there is no value in
    // drawing a minute of history one column at a time to catch up.
    if (queue.length > canvas.width) {
      queue = queue.slice(queue.length - canvas.width);
    }
  }

  function onMetrics(metrics: Map<number, Metrics>) {
    const nodeId = store.selectedNode.value;
    if (nodeId === null) return;
    const current = metrics.get(nodeId);
    const indices = current?.selection?.indices ?? current?.presence?.selected ?? [];
    selected = new Set(indices);
  }

  let animation = 0;
  function tick() {
    if (fitCanvas(canvas)) {
      // The backing store was resized, which cleared it. History lives in the canvas, so it is
      // simply gone; the alternative is keeping a parallel copy of every column, which costs
      // more than a resize is worth.
      framesDrawn = 0;
    }
    fitCanvas(overlay);
    drawColumns();
    drawOverlay();

    const agcAge = (performance.now() - lastAgcAt) / 1000;
    status.textContent =
      nSub === 0
        ? "waiting for frames"
        : `${nSub} subcarriers · ${store.rate.value} fps · ` +
          (lastAgcAt === 0 ? "no AGC steps yet" : `last AGC step ${agcAge.toFixed(0)}s ago`);

    animation = requestAnimationFrame(tick);
  }

  const controls = el(
    "div",
    { class: "controls" },
    select({
      label: "Colour",
      value: options.colormap,
      options: [
        { value: "viridis", label: "Viridis" },
        { value: "inferno", label: "Inferno" },
        { value: "gray", label: "Gray" },
      ],
      onChange: (value) => {
        options.colormap = value as ColormapName;
        legend.style.background = colormapGradient(options.colormap);
      },
      hint: "Perceptually uniform maps only — a rainbow invents edges that are not in the data",
    }),
    slider({
      label: "Contrast",
      min: 0.5,
      max: 8,
      step: 0.1,
      value: options.range,
      format: (v) => `±${v.toFixed(1)}σ`,
      onInput: (value) => (options.range = value),
      hint: "Colour range in standard deviations from each subcarrier's running mean",
    }),
    toggle({
      label: "Per-subcarrier normalize",
      value: options.normalize,
      onChange: (value) => (options.normalize = value),
      hint: "Off shows raw amplitude — useful once, to see how much the band varies",
    }),
    toggle({
      label: "Mark AGC steps",
      value: options.showAgc,
      onChange: (value) => (options.showAgc = value),
      hint: "Columns where the receiver changed gain; excluded from variance server-side",
    }),
    toggle({
      label: "Show selected subcarriers",
      value: options.showSelected,
      onChange: (value) => (options.showSelected = value),
    }),
    toggle({
      label: "Running",
      value: true,
      onChange: (value) => (running = value),
    }),
  );

  const legend = el("div", { class: "legend-bar" });
  legend.style.background = colormapGradient(options.colormap);

  const root = el(
    "div",
    { class: "view view-waterfall" },
    el(
      "div",
      { class: "panel panel-grow" },
      el(
        "div",
        { class: "panel-head" },
        el("h2", {}, "Waterfall"),
        el("div", { class: "panel-note" }, status),
      ),
      el(
        "div",
        { class: "waterfall-frame" },
        el("div", { class: "waterfall-canvases" }, canvas, overlay),
        el(
          "div",
          { class: "legend" },
          el("span", {}, "high"),
          legend,
          el("span", {}, "low"),
        ),
      ),
    ),
    el(
      "div",
      { class: "panel" },
      el("div", { class: "panel-head" }, el("h2", {}, "Display")),
      controls,
      el(
        "p",
        { class: "hint" },
        "Wave an arm between the nodes. If nothing moves here, stop and fix the capture chain — " +
          "no downstream processing rescues a bad signal, and you want to know that in an " +
          "evening rather than a month.",
      ),
    ),
  );

  let unsubscribeFrames: (() => void) | null = null;
  let unsubscribeMetrics: (() => void) | null = null;

  return {
    id: "waterfall",
    title: "Waterfall",
    root,
    mount() {
      unsubscribeFrames = store.onFrames(onFrames);
      unsubscribeMetrics = store.metrics.subscribe(onMetrics);
      animation = requestAnimationFrame(tick);
    },
    unmount() {
      cancelAnimationFrame(animation);
      unsubscribeFrames?.();
      unsubscribeMetrics?.();
      queue = [];
    },
  };
}
