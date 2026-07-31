// Selected subcarriers over time, overlaid.
//
// The waterfall shows everything at once and is the right view for "is there a signal". This is
// the right view for "what is the signal doing" — a handful of traces where you can see the
// actual shape of a breath, or watch one carrier saturate while its neighbour behaves.
//
// It defaults to the subcarriers the analysis has selected, so what you are looking at is what
// the BPM number is computed from rather than an unrelated sample.

import { el, fitCanvas, slider, toggle, viewLayout } from "../lib/dom";
import type { FrameBatch, Metrics } from "../lib/messages";
import { SERIES_COLORS, beginPlot, drawFrame, drawSeries } from "../lib/plot";
import { store } from "../lib/store";
import type { View } from "./view";

const HISTORY = 2400; // ~30 s at 80 Hz

export function subcarrierView(): View {
  const canvas = el("canvas", { class: "plot-canvas" });
  const legend = el("div", { class: "legend-list" });
  let windowSeconds = 20;
  let followSelection = true;
  let manual: number[] = [];

  // One ring per tracked subcarrier. Kept here rather than in the store because this is the
  // only view that wants per-subcarrier history in this shape.
  let tracked: number[] = [];
  let history = new Map<number, Float32Array>();
  let times = new Float32Array(HISTORY);
  let write = 0;
  let filled = 0;

  function setTracked(next: number[]) {
    const changed =
      next.length !== tracked.length || next.some((value, index) => value !== tracked[index]);
    if (!changed) return;

    tracked = next;
    history = new Map(next.map((sub) => [sub, new Float32Array(HISTORY).fill(NaN)]));
    write = 0;
    filled = 0;
    renderLegend();
  }

  function renderLegend() {
    legend.replaceChildren(
      ...tracked.map((sub, index) =>
        el(
          "span",
          { class: "legend-item" },
          el("i", { style: `background:${SERIES_COLORS[index % SERIES_COLORS.length]}` }),
          `#${sub}`,
        ),
      ),
    );
  }

  function onFrames(batch: FrameBatch) {
    if (tracked.length === 0) return;
    for (let i = 0; i < batch.count; i++) {
      const base = i * batch.nSub;
      for (const sub of tracked) {
        const series = history.get(sub);
        if (series === undefined) continue;
        series[write] = sub < batch.nSub ? batch.amp[base + sub] : NaN;
      }
      times[write] = batch.timestamps[i] / 1e6;
      write = (write + 1) % HISTORY;
      filled = Math.min(filled + 1, HISTORY);
    }
  }

  function onMetrics(metrics: Map<number, Metrics>) {
    if (!followSelection) return;
    const nodeId = store.selectedNode.value;
    if (nodeId === null) return;
    const current = metrics.get(nodeId);
    const selected = current?.selection?.indices ?? current?.presence?.selected ?? [];
    setTracked(selected.slice(0, 6));
  }

  function draw() {
    fitCanvas(canvas);
    const plot = beginPlot(canvas);
    if (plot === null || filled === 0 || tracked.length === 0) {
      if (plot !== null) {
        drawFrame(plot, [], []);
      }
      return;
    }

    // Take the newest `count` samples, oldest first.
    const newestTime = times[(write - 1 + HISTORY) % HISTORY];
    let count = 0;
    while (count < filled) {
      const index = (write - 1 - count + HISTORY * 2) % HISTORY;
      if (newestTime - times[index] > windowSeconds) break;
      count += 1;
    }
    if (count < 2) return;

    // Scale every trace to a common range so their shapes can be compared. Each is offset into
    // its own lane rather than overlapped, because six normalized traces on one axis is an
    // unreadable tangle and the interesting comparison is shape, not level.
    const lanes = tracked.length;
    tracked.forEach((sub, laneIndex) => {
      const series = history.get(sub);
      if (series === undefined) return;

      const values = new Float32Array(count);
      let min = Infinity;
      let max = -Infinity;
      for (let i = 0; i < count; i++) {
        const value = series[(write - count + i + HISTORY) % HISTORY];
        values[i] = value;
        if (Number.isFinite(value)) {
          if (value < min) min = value;
          if (value > max) max = value;
        }
      }
      if (!Number.isFinite(min) || max - min < 1e-9) return;

      const laneHeight = 1 / lanes;
      const bottom = 1 - (laneIndex + 1) * laneHeight;
      for (let i = 0; i < count; i++) {
        values[i] = bottom + ((values[i] - min) / (max - min)) * laneHeight * 0.86 + laneHeight * 0.07;
      }

      drawSeries(plot, values, { color: SERIES_COLORS[laneIndex % SERIES_COLORS.length] });
    });

    drawFrame(
      plot,
      [
        [0, `-${windowSeconds}s`],
        [0.5, `-${(windowSeconds / 2).toFixed(0)}s`],
        [1, "now"],
      ],
      tracked.map((sub, index) => {
        const laneHeight = 1 / tracked.length;
        return [1 - (index + 0.5) * laneHeight, `#${sub}`] as [number, string];
      }),
    );
  }

  const controls = el(
    "div",
    { class: "controls" },
    slider({
      label: "Window",
      min: 2,
      max: 30,
      step: 1,
      value: windowSeconds,
      format: (v) => `${v}s`,
      onInput: (value) => (windowSeconds = value),
    }),
    toggle({
      label: "Follow analysis selection",
      value: followSelection,
      onChange: (value) => {
        followSelection = value;
        if (!value && manual.length > 0) setTracked(manual);
      },
      hint: "Off pins the traces so you can watch one carrier while the ranking moves on",
    }),
    el(
      "label",
      { class: "control" },
      el("span", { class: "control-label" }, "Manual subcarriers"),
      el("input", {
        type: "text",
        placeholder: "e.g. 10, 12, 40",
        oninput: (event: Event) => {
          const raw = (event.target as HTMLInputElement).value;
          manual = raw
            .split(/[,\s]+/)
            .map((part) => Number.parseInt(part, 10))
            .filter((value) => Number.isInteger(value) && value >= 0 && value < 512)
            .slice(0, 8);
          if (!followSelection) setTracked(manual);
        },
      }),
    ),
  );

  const root = viewLayout({
    controlsTitle: "Traces",
    main: [
      el(
        "div",
        { class: "panel panel-grow" },
        el("div", { class: "panel-head" }, el("h2", {}, "Subcarrier lines")),
        el("div", { class: "plot-frame" }, canvas),
        // Below the plot rather than beside the title: six wrapping legend entries in a panel
        // head push the title onto its own line on a phone.
        legend,
      ),
    ],
    side: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Traces")),
        controls,
      ),
    ],
  });

  let animation = 0;
  let unsubscribeFrames: (() => void) | null = null;
  let unsubscribeMetrics: (() => void) | null = null;

  function tick() {
    draw();
    animation = requestAnimationFrame(tick);
  }

  return {
    id: "subcarriers",
    title: "Subcarriers",
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
    },
  };
}
