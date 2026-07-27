// The placement tuner: one enormous number that gets bigger when the node is in a better spot.
//
// Fresnel-zone theory says detectability depends on where the chest sits relative to the zone
// boundaries — best mid-zone, degrading at the boundaries — and that centimetre-scale position
// changes flip performance while moving closer to the line of sight does not reliably help. The
// theory's own conclusion is that trial and error is the correct method.
//
// So this view has one job: make trial and error fast. Big number, continuous update, a short
// history so you can tell whether the last move helped, and nothing else competing for
// attention while you are across the room holding a board.

import { el, fitCanvas } from "../lib/dom";
import type { Metrics } from "../lib/messages";
import { beginPlot, drawFrame, drawSeries } from "../lib/plot";
import { store } from "../lib/store";
import type { View } from "./view";

const HISTORY = 600; // two minutes at the 5 Hz metrics rate

export function placementView(): View {
  const value = el("div", { class: "tuner-value" }, "—");
  const unit = el("div", { class: "tuner-unit" }, "dB in-band SNR");
  const verdict = el("div", { class: "tuner-verdict" }, "waiting for data");
  const canvas = el("canvas", { class: "plot-canvas" });
  const best = el("div", { class: "panel-note" });

  const history = new Float32Array(HISTORY).fill(NaN);
  let write = 0;
  let filled = 0;
  let bestSeen = -Infinity;
  let worstSeen = Infinity;

  function onMetrics(metrics: Map<number, Metrics>) {
    const nodeId = store.selectedNode.value;
    if (nodeId === null) return;
    const placement = metrics.get(nodeId)?.placement;
    if (placement === undefined) return;

    const snr = placement.breathing_snr_db;
    history[write] = snr;
    write = (write + 1) % HISTORY;
    filled = Math.min(filled + 1, HISTORY);

    const isBest = snr > bestSeen;
    if (isBest) bestSeen = snr;
    if (snr < worstSeen) worstSeen = snr;

    value.textContent = snr.toFixed(1);

    // The verdict is relative to this session, not against fixed thresholds. There is no
    // absolute "good" value to compare against: the floor depends on the hardware, the room,
    // and how much low-frequency structure the channel has with nobody in it — measured on
    // synthetic scenes it sits around 9 dB, and strong respiration only reaches about 15. A
    // fixed threshold would therefore be wrong on someone else's setup in one direction or the
    // other. What is meaningful, and what the Fresnel-zone argument actually calls for, is
    // whether *this* position beats the last one.
    const span = bestSeen - worstSeen;
    if (filled < 12 || span < 1.5) {
      value.style.color = "#8b93a3";
      verdict.textContent = "move the node — readings need something to compare against";
    } else if (isBest) {
      value.style.color = "#06d6a0";
      verdict.textContent = "best position so far — mark this spot";
    } else if (snr >= bestSeen - 0.25 * span) {
      value.style.color = "#06d6a0";
      verdict.textContent = `near your best (${bestSeen.toFixed(1)} dB)`;
    } else if (snr >= worstSeen + 0.5 * span) {
      value.style.color = "#ffd166";
      verdict.textContent = `${(bestSeen - snr).toFixed(1)} dB below your best — keep trying`;
    } else {
      value.style.color = "#ef476f";
      verdict.textContent = "among the worst you have seen — move it";
    }

    best.textContent =
      `session range ${worstSeen.toFixed(1)} to ${bestSeen.toFixed(1)} dB` +
      ` · heart band ${placement.heart_snr_db.toFixed(1)} dB`;
  }

  function draw() {
    fitCanvas(canvas);
    const plot = beginPlot(canvas);
    if (plot === null) return;
    if (filled < 2) {
      drawFrame(plot, [], []);
      return;
    }

    const ordered = new Float32Array(filled);
    let min = Infinity;
    let max = -Infinity;
    for (let i = 0; i < filled; i++) {
      const sample = history[(write - filled + i + HISTORY) % HISTORY];
      ordered[i] = sample;
      if (sample < min) min = sample;
      if (sample > max) max = sample;
    }
    // A floor of a few dB of span, so a steady signal does not get amplified into a wobbling
    // trace that reads as instability.
    const span = Math.max(max - min, 6);
    const low = min - span * 0.1;

    const scaled = new Float32Array(filled);
    for (let i = 0; i < filled; i++) scaled[i] = (ordered[i] - low) / (span * 1.2);

    drawSeries(plot, scaled, { color: "#4cc9f0", width: 2, fill: "rgba(76, 201, 240, 0.14)" });
    drawFrame(
      plot,
      [
        [0, "-2min"],
        [1, "now"],
      ],
      [
        [0, `${low.toFixed(0)} dB`],
        [1, `${(low + span * 1.2).toFixed(0)} dB`],
      ],
    );
  }

  const root = el(
    "div",
    { class: "view view-tuner" },
    el(
      "div",
      { class: "panel panel-tuner" },
      el("div", { class: "tuner" }, value, unit, verdict),
      best,
    ),
    el(
      "div",
      { class: "panel panel-grow" },
      el("div", { class: "panel-head" }, el("h2", {}, "Last two minutes")),
      el("div", { class: "plot-frame" }, canvas),
      el(
        "p",
        { class: "hint" },
        "Move the node, wait a few seconds for the window to fill, read the number. Centimetres " +
          "matter and body orientation matters; moving closer to the line of sight often does " +
          "not help. Trial and error is the method the theory recommends — this view is here to " +
          "make it quick.",
      ),
      el(
        "button",
        { class: "button", onclick: () => {
            bestSeen = -Infinity;
            worstSeen = Infinity;
          },
        },
        "Reset range",
      ),
    ),
  );

  let animation = 0;
  let unsubscribe: (() => void) | null = null;

  function tick() {
    draw();
    animation = requestAnimationFrame(tick);
  }

  return {
    id: "placement",
    title: "Placement",
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
