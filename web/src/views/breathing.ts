// Phase 5 on screen: the filtered waveform, the spectrum it was picked from, and the BPM.
//
// All three together, deliberately. A BPM number on its own is unfalsifiable — it always looks
// like an answer. Next to a waveform with visible breaths and a spectrum with one clear peak it
// is checkable, and next to a flat waveform and a spectrum full of humps it is visibly not.
//
// The window slider is the important control. Published respiration MAE goes from 0.61
// breaths/min at a 1 s window to 0.09 at 20 s, and a 1 s window barely contains one breath.
// Drag it and watch the peak sharpen; that is a faster route to understanding this phase than
// any amount of reading.

import { el, fitCanvas, hintBlock, segmented, slider, viewLayout } from "../lib/dom";
import type { Metrics, VitalsState } from "../lib/messages";
import { beginPlot, drawBand, drawFrame, drawSeries, plotArea } from "../lib/plot";
import { store } from "../lib/store";
import type { View } from "./view";

type Band = "breathing" | "heart";

export function vitalsView(band: Band): View {
  const isHeart = band === "heart";
  const waveCanvas = el("canvas", { class: "plot-canvas" });
  const spectrumCanvas = el("canvas", { class: "plot-canvas" });
  const bpmValue = el("div", { class: "bpm-value" }, "—");
  const bpmUnit = el("div", { class: "bpm-unit" }, isHeart ? "BPM" : "breaths/min");
  // Confidence is what makes the number checkable, so it gets a colour of its own rather than
  // being the third clause of a sentence you have to read to the end of.
  const confidence = el("span", { class: "pill" }, "—");
  const detail = el("div", { class: "panel-note" });
  const controls = el("div", { class: "controls" });

  let latest: VitalsState | null = null;

  function onMetrics(metrics: Map<number, Metrics>) {
    const nodeId = store.selectedNode.value;
    if (nodeId === null) return;
    const current = metrics.get(nodeId);
    latest = (isHeart ? current?.heart : current?.breathing) ?? null;

    if (latest === null) {
      bpmValue.textContent = "—";
      bpmValue.style.color = "";
      confidence.className = "pill";
      confidence.textContent = "no estimate";
      // A blank panel reads as a broken server. When the server declined for a reason worth
      // acting on — a hole in the link long enough that interpolating across it would have
      // invented a breathing peak — say so, because the fix is on the network, not here.
      const rejected = isHeart ? current?.heart_rejected : current?.breathing_rejected;
      detail.textContent = rejected ?? "not enough history in the window yet";
      return;
    }

    bpmValue.textContent = latest.bpm.toFixed(1);
    // Led by stability, not by confidence. Confidence is the share of in-band energy in the
    // peak, which sounds like a trust score and is not one: measured against a known signal on
    // real hardware it came out *higher* for noise (0.47) than for a correct detection (0.36).
    // The spread of the recent run is what separated them, by a factor of thirty, and it is
    // also the thing the server gates on — so it is what the number is judged by here.
    const margin = latest.stability_tolerance > 0
      ? latest.stability_sd / latest.stability_tolerance
      : 0;
    const quality = margin < 0.5 ? "good" : margin < 0.8 ? "warn" : "bad";
    bpmValue.style.color = `var(--${quality})`;
    confidence.className = `pill pill-${quality}`;
    confidence.textContent = `±${latest.stability_sd.toFixed(1)} BPM`;
    detail.textContent =
      `steady to ±${latest.stability_sd.toFixed(2)} of ±${latest.stability_tolerance.toFixed(2)} allowed` +
      ` · ${latest.window_s.toFixed(0)}s window · peak share ${(latest.confidence * 100).toFixed(0)}%` +
      ` · in-band SNR ${latest.snr_db.toFixed(1)} dB`;
  }

  function drawWaveform() {
    fitCanvas(waveCanvas);
    const plot = beginPlot(waveCanvas);
    if (plot === null) return;
    if (latest === null || latest.waveform.length < 2) {
      drawFrame(plot, [], []);
      return;
    }

    const points = latest.waveform.map((value) => value / 2 + 0.5);
    drawSeries(plot, points, { color: "#4cc9f0", width: 1.6 });

    const seconds = latest.waveform.length / Math.max(latest.waveform_fs, 1e-6);
    drawFrame(
      plot,
      [
        [0, `-${seconds.toFixed(0)}s`],
        [1, "now"],
      ],
      [
        [0.5, "0"],
        [1, "+1"],
        [0, "-1"],
      ],
    );
  }

  function drawSpectrum() {
    fitCanvas(spectrumCanvas);
    const plot = beginPlot(spectrumCanvas);
    if (plot === null) return;
    if (latest === null || latest.spectrum.length < 2) {
      drawFrame(plot, [], []);
      return;
    }

    const max = Math.max(...latest.spectrum) || 1;
    const normalized = latest.spectrum.map((value) => value / max);
    drawSeries(plot, normalized, {
      color: "#ffd166",
      width: 1.4,
      fill: "rgba(255, 209, 102, 0.16)",
    });

    // Mark where the peak was picked, so a spectrum with two comparable humps is obviously a
    // spectrum with two comparable humps rather than a confident wrong answer.
    const peakHz = latest.bpm / 60;
    const lo = latest.freqs[0];
    const hi = latest.freqs[latest.freqs.length - 1];
    const position = (peakHz - lo) / Math.max(hi - lo, 1e-9);
    if (position >= 0 && position <= 1) {
      const area = plotArea(plot);
      plot.context.strokeStyle = "rgba(6, 214, 160, 0.9)";
      plot.context.lineWidth = plot.ratio;
      plot.context.beginPath();
      plot.context.moveTo(area.x0 + position * area.w, area.y0);
      plot.context.lineTo(area.x0 + position * area.w, area.y1);
      plot.context.stroke();
    }

    drawBand(plot, 0, 1, "rgba(255,255,255,0.02)");
    drawFrame(
      plot,
      [
        [0, `${(lo * 60).toFixed(0)}`],
        [0.5, `${(((lo + hi) / 2) * 60).toFixed(0)}`],
        [1, `${(hi * 60).toFixed(0)} ${isHeart ? "BPM" : "br/min"}`],
      ],
      [[1, "peak"]],
    );
  }

  function buildControls() {
    const config = store.config.value;
    const settings = isHeart ? config?.heart : config?.breathing;
    controls.replaceChildren(
      slider({
        label: "Window",
        min: isHeart ? 2 : 2,
        max: isHeart ? 20 : 40,
        step: 1,
        value: settings?.window_s ?? (isHeart ? 5 : 20),
        format: (v) => `${v}s`,
        onInput: (value) => store.patchConfig({ [band]: { window_s: value } }),
        hint: isHeart
          ? "A 1 s window may contain zero heartbeats. 5 s is the published starting point."
          : "The dominant parameter. 15-20 s is the working range; watch the peak sharpen.",
      }),
      slider({
        label: "Subcarriers combined",
        min: 1,
        max: 16,
        step: 1,
        value: settings?.n_subcarriers ?? (isHeart ? 12 : 8),
        onInput: (value) => store.patchConfig({ [band]: { n_subcarriers: value } }),
        hint: "Averaging independent estimates beats trusting the single best carrier",
      }),
      slider({
        label: "Magnitude gate",
        min: 0,
        max: 0.8,
        step: 0.05,
        value: settings?.gate_quantile ?? 0.25,
        format: (v) => `bottom ${(v * 100).toFixed(0)}%`,
        onInput: (value) => store.patchConfig({ [band]: { gate_quantile: value } }),
      }),
      hintBlock(
        isHeart
          ? "Expect this to work only for a near-motionless person at close range. The cardiac " +
              "signal is far weaker than respiration and gets masked by it. Published results " +
              "plateau near 0.50 BPM error on 64-subcarrier hardware regardless of window " +
              "length — that wall is the subcarrier count, not the code."
          : "Blind spots are unavoidable with amplitude-only single-antenna hardware. The " +
              "published fix needs two antennas on one radio; here the mitigation is more nodes " +
              "covering more geometry. Use the placement tuner to find a good spot.",
      ),
    );
  }

  const root = viewLayout({
    controlsTitle: "Analysis",
    main: [
      el(
        "div",
        { class: "panel" },
        el(
          "div",
          { class: "panel-head" },
          // The only band selector, on every layout: the phone reaches both bands through one
          // Vitals tab and the desktop sidebar through one Vitals entry. Listing the bands in
          // the sidebar as well put the same choice on screen twice.
          segmented({
            value: band,
            label: "Band",
            options: [
              { value: "breathing", label: "Breathing" },
              { value: "heart", label: "Heart" },
            ],
            onChange: (value) => {
              if (value !== band) location.hash = value;
            },
          }),
          el("div", { class: "spacer" }),
          confidence,
        ),
        el("div", { class: "bpm" }, bpmValue, bpmUnit),
        detail,
      ),
      el(
        "div",
        { class: "panel panel-grow" },
        el("div", { class: "panel-head" }, el("h3", {}, "Filtered waveform")),
        el("div", { class: "plot-frame" }, waveCanvas),
        el("div", { class: "panel-head" }, el("h3", {}, "Spectrum")),
        el("div", { class: "plot-frame" }, spectrumCanvas),
      ),
    ],
    side: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Analysis")),
        controls,
      ),
    ],
  });

  let animation = 0;
  let unsubscribeMetrics: (() => void) | null = null;
  let unsubscribeConfig: (() => void) | null = null;

  function tick() {
    drawWaveform();
    drawSpectrum();
    animation = requestAnimationFrame(tick);
  }

  return {
    id: band,
    title: isHeart ? "Heart" : "Breathing",
    root,
    mount() {
      buildControls();
      unsubscribeMetrics = store.metrics.subscribe(onMetrics);
      // Rebuild when the server confirms a config change, so the sliders reflect what is
      // actually running rather than what was last dragged.
      unsubscribeConfig = store.config.subscribe(() => buildControls(), false);
      animation = requestAnimationFrame(tick);
    },
    unmount() {
      cancelAnimationFrame(animation);
      unsubscribeMetrics?.();
      unsubscribeConfig?.();
    },
  };
}
