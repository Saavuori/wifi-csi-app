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
//
// The columns are also kept as data, in a `ColumnStore`, and not only as pixels. Pixels alone
// made two things impossible that a user reasonably expects: opening the page drew an empty
// plot that took a screen-width of frames to say anything, and whatever had scrolled off the
// left was gone. So the view backfills the server's ring on mount and can be scrubbed back
// through what it holds. Point 2 above survives intact — following live is still an offset blit
// of only the new columns; a full repaint happens when the user is scrubbing, where it costs
// one frame per change of position rather than one per column.
//
// Rewind here is bounded by memory, which is minutes. Going back further is what replaying a
// recorded session is for, and that path already exists.

import { ColumnStore } from "../lib/columns";
import { colormap, colormapGradient, type ColormapName } from "../lib/colormap";
import {
  ICONS,
  el,
  fitCanvas,
  fullscreen,
  hintBlock,
  icon,
  select,
  slider,
  toggle,
  viewLayout,
} from "../lib/dom";
import type { FrameBatch, Metrics } from "../lib/messages";
import { decodeHistory } from "../lib/protocol";
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

  // Everything the view has seen, as data rather than pixels. Rebuilt when the subcarrier count
  // changes, because a column is a fixed-width row in it.
  let columns: ColumnStore | null = null;
  // Columns appended since the last repaint — the width of the incremental blit while live.
  let pending = 0;
  // Device timestamp of the newest column held, so backfill and the live stream can overlap
  // without drawing the same moment twice.
  let newestAt = Number.NEGATIVE_INFINITY;

  let scratch: ImageData | null = null;
  let lastAgcAt = 0;
  let selected = new Set<number>();

  // Transport. `live` follows the newest column; otherwise `viewEnd` is the logical index of the
  // newest *visible* column and the canvas is repainted from the store whenever it moves.
  let live = true;
  let viewEnd = 0;
  let repaintWanted = false;
  let scrubbing = false;

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

  function resetColumns(count: number) {
    columns = new ColumnStore(count);
    newestAt = Number.NEGATIVE_INFINITY;
    pending = 0;
    viewEnd = 0;
    live = true;
    // What is on the canvas belongs to the history just discarded. Without a full repaint the
    // incremental path would scroll it along a column at a time, mixing two different rooms.
    repaintWanted = true;
  }

  function ensureStore(count: number) {
    if (columns === null || columns.nSub !== count) resetColumns(count);
  }

  /**
   * Append one column, unless it is one we already hold.
   *
   * The backfill and the live stream overlap by however long the request took, so without this
   * the same half-second of room is drawn twice, side by side, as a visible seam. A timestamp
   * far *behind* the newest is a different thing — the node rebooted and its clock restarted —
   * and the history before that reset no longer joins onto what follows.
   */
  function pushColumn(amp: Float32Array, timestamp: number, agc: boolean) {
    if (columns === null) return;
    if (timestamp <= newestAt) {
      if (timestamp > newestAt - 5e6) return; // already held
      resetColumns(columns.nSub);
    }

    const before = columns.count;
    updateStats(amp, before > 0);
    columns.push(amp, timestamp, agc);
    newestAt = timestamp;
    pending += 1;
    // A full ring drops its oldest column per push, which shifts every logical index down by
    // one. While scrubbing, the position has to move with the data or the view slides forward
    // on its own.
    if (!live && columns.count === before) viewEnd = Math.max(0, viewEnd - 1);
    if (agc) lastAgcAt = performance.now();
  }

  /** Render `count` columns starting at logical index `from` into the canvas at `destX`. */
  function blit(from: number, count: number, destX: number) {
    if (context === null || columns === null || count <= 0) return;
    if (mean === null || variance === null) return;

    const height = canvas.height;
    if (scratch === null || scratch.width !== count || scratch.height !== height) {
      scratch = context.createImageData(count, height);
    }

    const lut = colormap(options.colormap);
    const pixels = scratch.data;
    const scale = height / nSub;

    for (let c = 0; c < count; c++) {
      const amp = columns.column(from + c);
      const agc = columns.agc(from + c);
      for (let y = 0; y < height; y++) {
        // Flip so low subcarrier indices sit at the bottom, matching the axis labels.
        const sub = Math.min(nSub - 1, Math.floor((height - 1 - y) / scale));
        const value = amp[sub];
        const offset = (y * count + c) * 4;

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

    context.putImageData(scratch, destX, 0);
  }

  /** Follow the newest data: scroll what is drawn and paint only the columns that are new. */
  function followLive() {
    if (context === null || columns === null || pending === 0) return;
    const width = canvas.width;
    // Also bounded by what is held: a tab left in the background can accumulate more pending
    // columns than the ring kept, and the difference would index off the front of it.
    const count = Math.min(pending, width, columns.count);
    pending = 0;

    // Scroll the existing image left by `count` pixels by drawing the canvas onto itself. This
    // is the one trick that keeps a long session cheap: history is not re-rendered to advance.
    context.globalCompositeOperation = "copy";
    context.drawImage(canvas, -count, 0);
    context.globalCompositeOperation = "source-over";

    blit(columns.count - count, count, width - count);
    viewEnd = columns.count - 1;
  }

  /** Repaint the whole visible window from the store, for a view that has been scrubbed. */
  function paintWindow() {
    if (context === null || columns === null) return;
    const width = canvas.width;
    const end = Math.min(viewEnd, columns.count - 1);
    const count = Math.min(width, end + 1);

    // Short history is drawn against the right edge, so "now" is always the same place on
    // screen whether the buffer is full or the app opened a moment ago.
    context.fillStyle = "#1a1c22";
    context.fillRect(0, 0, width, canvas.height);
    blit(end - count + 1, count, width - count);
    pending = 0;
  }

  function drawOverlay() {
    if (overlayContext === null) return;
    const width = overlay.width;
    const height = overlay.height;
    overlayContext.clearRect(0, 0, width, height);
    if (nSub === 0) return;

    const ratio = window.devicePixelRatio || 1;
    overlayContext.font = `${(height < 320 * ratio ? 10 : 11) * ratio}px ui-monospace, monospace`;
    overlayContext.textBaseline = "middle";

    // Subcarrier axis. Every 8 indices is dense enough to locate a feature and sparse enough to
    // stay readable — on a desktop panel. On a phone, or with a Pi's 256 subcarriers, that same
    // spacing stacks labels on top of one another, so the step is whatever keeps roughly 22 px
    // between gridlines instead.
    let step = 8;
    while (step < nSub && (step / nSub) * height < 22 * ratio) step *= 2;

    overlayContext.strokeStyle = "rgba(255,255,255,0.08)";
    for (let sub = 0; sub < nSub; sub += step) {
      const y = height - 1 - (sub / nSub) * height;
      overlayContext.beginPath();
      overlayContext.moveTo(0, y);
      overlayContext.lineTo(width, y);
      overlayContext.stroke();
    }

    // Labels second, and haloed. They sit directly on the data — there is no gutter to put them
    // in without spending width the waterfall wants for time — and pale grey on a bright viridis
    // green is invisible exactly when the plot is busiest.
    overlayContext.lineWidth = 3 * ratio;
    overlayContext.lineJoin = "round";
    overlayContext.strokeStyle = "rgba(0,0,0,0.6)";
    overlayContext.fillStyle = "rgba(255,255,255,0.85)";
    for (let sub = 0; sub < nSub; sub += step) {
      const y = height - 1 - (sub / nSub) * height;
      overlayContext.strokeText(String(sub), 4 * ratio, y - 8 * ratio);
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
    if (batch.nSub !== nSub) resetStats(batch.nSub);
    ensureStore(batch.nSub);

    for (let i = 0; i < batch.count; i++) {
      // `push` copies into the ring, so this view onto the transferred buffer does not outlive
      // the call and nothing here needs its own copy.
      const amp = batch.amp.subarray(i * batch.nSub, (i + 1) * batch.nSub);
      pushColumn(amp, batch.timestamps[i], batch.agc[i] === 1);
    }
  }

  /**
   * Fill the view from the server's ring, so a browser that has just connected shows the last
   * couple of minutes rather than an empty canvas that takes a screen-width of frames to say
   * anything. Best effort: this is a nicety, and a node that has sent nothing yet answers 204.
   */
  async function backfill(nodeId: number) {
    let history;
    try {
      const response = await fetch(`/api/nodes/${nodeId}/history`);
      if (!response.ok || response.status === 204) return;
      history = decodeHistory(await response.arrayBuffer());
    } catch {
      return; // offline, or navigated away mid-flight
    }
    if (history === null || history === undefined || history.count === 0) return;
    // The user may have switched nodes while the request was in flight, in which case this is
    // the wrong room's history.
    if (store.selectedNode.value !== nodeId) return;

    // The store is replaced rather than prepended to: it is append-only, and this data is older
    // than any live frame that arrived during the round trip. Those few columns are in the
    // block too — the ring they came from is what produced it — so the only loss is whatever
    // landed in the milliseconds after the server took its snapshot, and the live stream
    // continues from there on the next frame.
    if (history.nSub !== nSub) resetStats(history.nSub);
    resetColumns(history.nSub);
    for (let i = 0; i < history.count; i++) {
      const amp = history.amp.subarray(i * history.nSub, (i + 1) * history.nSub);
      pushColumn(amp, history.timestamps[i], history.agc[i] === 1);
    }
    repaintWanted = true;
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
      // Resizing the backing store clears it. The columns are held as data, so unlike before
      // this costs a repaint rather than the history.
      repaintWanted = true;
    }
    fitCanvas(overlay);

    // A resize, a backfill or a scrub needs the window rebuilt from the store; following live
    // needs only the new columns. Keeping the second case incremental is the whole reason a
    // long session stays cheap.
    if (repaintWanted) {
      if (live && columns !== null) viewEnd = columns.count - 1;
      paintWindow();
      repaintWanted = false;
    } else if (live) {
      followLive();
    }
    updateTransport();
    drawOverlay();

    const agcAge = (performance.now() - lastAgcAt) / 1000;
    status.textContent =
      nSub === 0
        ? "waiting for frames"
        : `${nSub} sub · ${store.rate.value} fps · ` +
          (lastAgcAt === 0 ? "no AGC yet" : `AGC ${agcAge.toFixed(0)}s ago`);

    animation = requestAnimationFrame(tick);
  }

  // -- transport -------------------------------------------------------------------------
  //
  // Under the plot rather than in the display controls at the side: this is not a preference
  // about how the data looks, it is where in the data you are, and it is the one control you
  // reach for while watching the plot rather than while setting it up.

  const scrubber = el("input", {
    type: "range",
    class: "waterfall-scrubber",
    min: 0,
    max: 0,
    step: 1,
    value: 0,
    "aria-label": "Position in the buffered history",
  });

  const position = el("span", { class: "waterfall-position" }, "live");

  const liveButton = el(
    "button",
    {
      class: "button button-small waterfall-live",
      type: "button",
      title: "Jump back to the newest data and follow it",
    },
    "Live",
  );

  function goLive() {
    live = true;
    repaintWanted = true;
  }

  function seekTo(index: number) {
    if (columns === null) return;
    live = false;
    viewEnd = Math.max(0, Math.min(index, columns.count - 1));
    repaintWanted = true;
  }

  scrubber.addEventListener("input", () => seekTo(Number(scrubber.value)));
  // While the pointer is down the input owns its own value; writing to it from the animation
  // frame would fight the drag.
  scrubber.addEventListener("pointerdown", () => (scrubbing = true));
  scrubber.addEventListener("pointerup", () => (scrubbing = false));
  scrubber.addEventListener("pointercancel", () => (scrubbing = false));
  liveButton.addEventListener("click", goLive);

  const followToggle = toggle({
    label: "Follow live",
    value: true,
    onChange: (value) => (value ? goLive() : seekTo(viewEnd)),
    hint: "Off freezes the view. The buffer keeps filling either way — scrub back with the bar under the plot",
  });
  // The same state as the transport bar, so the two cannot disagree: dragging the scrubber or
  // pressing Live has to move this, or it sits there claiming the view is following live while
  // the plot is thirty seconds behind.
  const followInput = followToggle.querySelector("input") as HTMLInputElement | null;

  function updateTransport() {
    const count = columns?.count ?? 0;
    const max = Math.max(0, count - 1);
    if (live) viewEnd = max;

    if (followInput !== null && followInput.checked !== live) followInput.checked = live;
    if (!scrubbing) {
      if (scrubber.max !== String(max)) scrubber.max = String(max);
      if (scrubber.value !== String(viewEnd)) scrubber.value = String(viewEnd);
    }
    scrubber.disabled = count === 0;
    liveButton.disabled = live;

    if (live) {
      position.textContent = "live";
      position.classList.remove("behind");
      return;
    }
    // Behind by the device clock, not by wall time: it is the one that is actually uniform, and
    // it is the same clock the recordings and the analysis windows are measured on.
    const behind = columns === null ? NaN : columns.span(viewEnd, count - 1);
    position.textContent = Number.isFinite(behind) ? `−${behind.toFixed(1)} s` : "paused";
    position.classList.add("behind");
  }

  const transport = el(
    "div",
    { class: "waterfall-transport" },
    liveButton,
    scrubber,
    position,
  );

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
        repaintWanted = true;
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
      // Repaint rather than letting the change apply only to columns that have not been drawn
      // yet: the point of turning contrast up is to see the feature already on screen.
      onInput: (value) => {
        options.range = value;
        repaintWanted = true;
      },
      hint: "Colour range in standard deviations from each subcarrier's running mean",
    }),
    toggle({
      label: "Per-subcarrier normalize",
      value: options.normalize,
      onChange: (value) => {
        options.normalize = value;
        repaintWanted = true;
      },
      hint: "Off shows raw amplitude — useful once, to see how much the band varies",
    }),
    toggle({
      label: "Mark AGC steps",
      value: options.showAgc,
      onChange: (value) => {
        options.showAgc = value;
        repaintWanted = true;
      },
      hint: "Columns where the receiver changed gain; excluded from variance server-side",
    }),
    toggle({
      label: "Show selected subcarriers",
      value: options.showSelected,
      onChange: (value) => (options.showSelected = value),
    }),
    followToggle,
  );

  const legend = el("div", { class: "legend-bar" });
  legend.style.background = colormapGradient(options.colormap);

  // The button's glyph and label track the real state rather than a local boolean, because the
  // user can leave fullscreen by pressing Escape or with a system gesture that never reaches us.
  const expandIcon = el("span", { class: "expand-icon" }, icon(ICONS.expand));
  const expandLabel = el("span", { class: "button-label" }, "Fullscreen");
  const expandButton = el(
    "button",
    {
      class: "button button-small waterfall-expand",
      type: "button",
      title: "Fill the screen with the waterfall",
      "aria-pressed": "false",
    },
    expandIcon,
    expandLabel,
  );

  const plot = el(
    "div",
    { class: "panel panel-grow waterfall-panel" },
    el(
      "div",
      { class: "panel-head" },
      el("h2", {}, "Waterfall"),
      el("div", { class: "spacer" }),
      el("div", { class: "panel-note" }, status),
      expandButton,
    ),
    el(
      "div",
      { class: "waterfall-frame" },
      el("div", { class: "waterfall-canvases" }, canvas, overlay),
      // Column on the desktop, a strip under the plot on a phone — where a 34 px-wide bar
      // would cost a tenth of the width the waterfall spends on time.
      el("div", { class: "legend" }, el("span", {}, "high"), legend, el("span", {}, "low")),
    ),
    transport,
  );

  const screen = fullscreen(plot, (on) => {
    expandIcon.replaceChildren(icon(on ? ICONS.collapse : ICONS.expand));
    expandLabel.textContent = on ? "Exit" : "Fullscreen";
    expandButton.setAttribute("aria-pressed", on ? "true" : "false");
    expandButton.title = on ? "Leave fullscreen (Esc)" : "Fill the screen with the waterfall";
  });
  expandButton.addEventListener("click", () => void screen.toggle());

  const root = viewLayout({
    className: "view-waterfall",
    controlsTitle: "Display",
    main: [plot],
    side: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Display")),
        controls,
        hintBlock(
          "Wave an arm between the nodes. If nothing moves here, stop and fix the capture chain — " +
            "no downstream processing rescues a bad signal, and you want to know that in an " +
            "evening rather than a month.",
        ),
      ),
    ],
  });

  let unsubscribeFrames: (() => void) | null = null;
  let unsubscribeMetrics: (() => void) | null = null;
  let unsubscribeNode: (() => void) | null = null;
  let backfilledNode: number | null = null;

  return {
    id: "waterfall",
    title: "Waterfall",
    root,
    mount() {
      unsubscribeFrames = store.onFrames(onFrames);
      unsubscribeMetrics = store.metrics.subscribe(onMetrics);
      // Fires immediately with the current selection, which is what backfills on open; after
      // that it is the node switch, whose history is a different room and has to be refetched.
      //
      // Once per node, though. This view is reused for the life of the app, so leaving another
      // tab and coming back remounts it — and refetching there would throw away however much
      // scrollback the browser had accumulated and replace it with the server's two minutes.
      unsubscribeNode = store.selectedNode.subscribe((nodeId) => {
        if (nodeId === null || nodeId === backfilledNode) return;
        backfilledNode = nodeId;
        void backfill(nodeId);
      });
      animation = requestAnimationFrame(tick);
    },
    unmount() {
      cancelAnimationFrame(animation);
      unsubscribeFrames?.();
      unsubscribeMetrics?.();
      unsubscribeNode?.();
      // Navigating away with the plot still expanded would otherwise strand the class on the
      // document — the same failure the sheets avoid by closing on navigation. Not disposed:
      // this view instance is reused for the life of the app and will be mounted again.
      if (screen.active()) void screen.toggle();
    },
  };
}
