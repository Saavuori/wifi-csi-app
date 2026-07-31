// Phase 4 on screen: the motion index over time, with the presence state and both hysteresis
// thresholds visible.
//
// Both thresholds are drawn, not just the one currently in play. The whole reason for hysteresis
// is that the enter and exit levels differ, and a plot that hides that makes every state change
// look either obvious or inexplicable. Seeing the gap is how you decide whether to widen it.

import { el, fitCanvas, hintBlock, slider, toggle, viewLayout } from "../lib/dom";
import type { Metrics } from "../lib/messages";
import { beginPlot, drawBand, drawFrame, drawHorizontal, drawSeries, plotArea } from "../lib/plot";
import { store } from "../lib/store";
import type { View } from "./view";

const HISTORY = 1800; // at the 5 Hz metrics rate, six minutes

const STATE_COLOR: Record<string, string> = {
  calibrating: "#ffd166",
  absent: "#4cc9f0",
  present: "#06d6a0",
};

export function motionView(): View {
  const canvas = el("canvas", { class: "plot-canvas" });
  const badge = el("div", { class: "state-badge" }, "—");
  const readout = el("div", { class: "panel-note" });

  const motion = new Float32Array(HISTORY).fill(NaN);
  const states: string[] = [];
  let write = 0;
  let filled = 0;
  let enter = 0;
  let exit = 0;
  let logScale = true;

  function onMetrics(metrics: Map<number, Metrics>) {
    const nodeId = store.selectedNode.value;
    if (nodeId === null) return;
    const presence = metrics.get(nodeId)?.presence;
    if (presence === undefined) return;

    motion[write] = presence.motion;
    states[write] = presence.state;
    write = (write + 1) % HISTORY;
    filled = Math.min(filled + 1, HISTORY);
    enter = presence.enter;
    exit = presence.exit;

    badge.textContent =
      presence.state === "calibrating"
        ? `calibrating ${(presence.calibration * 100).toFixed(0)}%`
        : presence.state;
    badge.style.background = STATE_COLOR[presence.state] ?? "#9aa5b1";

    readout.textContent =
      `motion ${presence.motion.toFixed(2)} · enter ${enter.toFixed(2)} · exit ${exit.toFixed(2)}` +
      ` · ${presence.since_s.toFixed(0)}s in state · ${presence.selected.length} subcarriers`;
  }

  // The motion index spans orders of magnitude — around 1 in an empty room and in the hundreds
  // with someone walking through the link — so a linear axis shows a flat line at the bottom
  // and a spike off the top. Log is the honest default here; linear is available for looking
  // closely at the quiet end.
  const scaleOf = (value: number, max: number) =>
    logScale
      ? Math.log10(Math.max(value, 0.05) + 1) / Math.log10(max + 1)
      : value / max;

  function draw() {
    fitCanvas(canvas);
    const plot = beginPlot(canvas);
    if (plot === null) return;

    if (filled < 2) {
      drawFrame(plot, [], []);
      return;
    }

    const ordered = new Float32Array(filled);
    for (let i = 0; i < filled; i++) {
      ordered[i] = motion[(write - filled + i + HISTORY) % HISTORY];
    }

    let max = Math.max(enter * 1.6, 2);
    for (const value of ordered) if (Number.isFinite(value) && value > max) max = value;
    max *= 1.05;

    // The hysteresis gap. Inside this band the state does not change, whichever way it is
    // heading, and that is the single most useful thing to see when tuning.
    drawBand(plot, scaleOf(exit, max), scaleOf(enter, max), "rgba(255, 209, 102, 0.10)");

    // Shade the stretches where presence was asserted, so the state and the metric that caused
    // it are on the same axis.
    const area = plotArea(plot);
    plot.context.fillStyle = "rgba(6, 214, 160, 0.14)";
    for (let i = 0; i < filled; i++) {
      if (states[(write - filled + i + HISTORY) % HISTORY] !== "present") continue;
      const x = area.x0 + (i / (filled - 1)) * area.w;
      plot.context.fillRect(x, area.y0, Math.max(1, area.w / filled + 1), area.h);
    }

    const scaled = new Float32Array(filled);
    for (let i = 0; i < filled; i++) scaled[i] = scaleOf(ordered[i], max);
    drawSeries(plot, scaled, { color: "#4cc9f0", width: 1.5 });

    drawHorizontal(plot, scaleOf(enter, max), "rgba(6, 214, 160, 0.8)");
    drawHorizontal(plot, scaleOf(exit, max), "rgba(76, 201, 240, 0.8)");

    const ticks: [number, string][] = logScale
      ? [1, 2, 5, 10, 50, 200]
          .filter((value) => value <= max)
          .map((value) => [scaleOf(value, max), String(value)])
      : [0, 0.25, 0.5, 0.75, 1].map((t) => [t, (t * max).toFixed(1)]);

    drawFrame(plot, [[0, "older"], [1, "now"]], ticks);
  }

  const config = store.config.value;
  const controls = el(
    "div",
    { class: "controls" },
    slider({
      label: "Enter threshold",
      min: 2,
      max: 20,
      step: 0.5,
      value: config?.presence.enter_sigma ?? 6,
      format: (v) => `${v.toFixed(1)}σ`,
      onInput: (value) => store.patchConfig({ presence: { enter_sigma: value } }),
      hint: "In robust standard deviations of the motion index measured during calibration",
    }),
    slider({
      label: "Exit threshold",
      min: 0.5,
      max: 10,
      step: 0.5,
      value: config?.presence.exit_sigma ?? 2.5,
      format: (v) => `${v.toFixed(1)}σ`,
      onInput: (value) => store.patchConfig({ presence: { exit_sigma: value } }),
      hint: "Keep it below the enter threshold — the gap is the hysteresis",
    }),
    slider({
      label: "Debounce",
      min: 0,
      max: 10,
      step: 0.5,
      value: config?.presence.debounce_s ?? 1.5,
      format: (v) => `${v.toFixed(1)}s`,
      onInput: (value) => store.patchConfig({ presence: { debounce_s: value } }),
      hint: "How long a threshold crossing must hold before the state changes",
    }),
    slider({
      label: "Variance window",
      min: 0.5,
      max: 10,
      step: 0.5,
      value: config?.presence.window_s ?? 2,
      format: (v) => `${v.toFixed(1)}s`,
      onInput: (value) => store.patchConfig({ presence: { window_s: value } }),
    }),
    slider({
      label: "Magnitude gate",
      min: 0,
      max: 0.8,
      step: 0.05,
      value: config?.presence.gate_quantile ?? 0.25,
      format: (v) => `bottom ${(v * 100).toFixed(0)}%`,
      onInput: (value) => store.patchConfig({ presence: { gate_quantile: value } }),
      hint: "Drops the weakest subcarriers, which are in deep fade and noise-dominated",
    }),
    toggle({
      label: "PCA aggregation",
      value: config?.presence.use_pca ?? true,
      onChange: (value) => store.patchConfig({ presence: { use_pca: value } }),
      hint: "Concentrates correlated motion into the leading components and leaves noise spread out",
    }),
    toggle({
      label: "Log scale",
      value: logScale,
      onChange: (value) => (logScale = value),
    }),
    el(
      "button",
      {
        class: "button button-wide",
        onclick: () => store.recalibrate(store.selectedNode.value),
      },
      "Recalibrate baseline",
    ),
    hintBlock(
      "Leave the room before recalibrating — establishing an empty-room baseline is the one " +
        "thing the detector cannot verify for itself. Expect to redo it after a few hours; the " +
        "channel drifts.",
    ),
  );

  const root = viewLayout({
    controlsTitle: "Detection",
    main: [
      el(
        "div",
        { class: "panel panel-grow" },
        el("div", { class: "panel-head" }, el("h2", {}, "Motion"), badge),
        el("div", { class: "plot-frame" }, canvas),
        readout,
      ),
    ],
    side: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Detection")),
        controls,
      ),
    ],
  });

  let animation = 0;
  let unsubscribe: (() => void) | null = null;

  function tick() {
    draw();
    animation = requestAnimationFrame(tick);
  }

  return {
    id: "motion",
    title: "Motion",
    root,
    mount() {
      unsubscribe = store.metrics.subscribe(onMetrics);
      animation = requestAnimationFrame(tick);
    },
    unmount() {
      cancelAnimationFrame(animation);
      unsubscribe?.();
    },
  };
}
