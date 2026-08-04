// What the capture is made of. Every row of the waterfall is one 802.11 frame that happened to
// be on the air, so the sample rate of every analysis in this app is a property of the room's
// traffic rather than of the server — and when breathing will not converge or the waterfall grows
// stripes, this is the page that says why.
//
// Two numbers do most of the work here, and they are not the same number. The rate is how many
// frames a second arrived; the duty cycle is how many of those seconds carried any at all. A link
// at 40 Hz and duty 1.0 draws an even waterfall and resamples cleanly. A link averaging 40 Hz in
// bursts, silent half the time, produces the identical rate and an unusable window — `Coverage`
// in the DSP layer exists precisely because interpolating across those holes puts invented power
// in the respiration band.
//
// The breakdown by transmitter is what makes it actionable. A node filtered to one BSSID lives
// and dies by that access point's beacon rate; an unfiltered monitor is sampling whatever the
// neighbours are doing, and seeing which MAC actually feeds the waterfall is how you decide
// whether to change channel, lock to another AP, or turn the stimulus on.

import { clear, el, fitCanvas, formatAgo, hintBlock, stat, viewLayout } from "../lib/dom";
import type { TrafficNode, TrafficSource } from "../lib/messages";
import { SERIES_COLORS, beginPlot, drawFrame, plotArea } from "../lib/plot";
import { store } from "../lib/store";
import type { View } from "./view";

// Seconds between polls. The server buckets by the second, so anything faster would redraw the
// same numbers; anything slower would make a stall that has just started look like it is still
// going when it has already cleared.
const POLL_MS = 1000;

// How many transmitters are listed before the rest are folded away. A busy channel produces a
// long tail of one-frame passers-by, and the whole page is about which sources matter.
const VISIBLE_SOURCES = 12;

// Sources beyond the palette, and the frames no source claimed, are drawn in this. Deliberately
// the muted grey rather than another hue: "not individually interesting" is the message.
const REST_COLOR = "#5b6474";

// The per-row sparkline, in CSS pixels. Sized here rather than in the stylesheet because the
// canvas is drawn the moment it is built, before it has been laid out — measuring its box would
// mean deferring to an animation frame, and a hidden tab runs no animation frames at all, so
// every row would stay blank until the tab came back.
const SPARK_W = 84;
const SPARK_H = 26;

function verdict(node: TrafficNode): { text: string; className: string } {
  if (node.frames_window === 0) return { text: "silent", className: "bad" };
  // Ranked above the rate checks on purpose. An intermittent capture is not a slow capture: the
  // fix is different (find out what stops the traffic) and the failure is worse, because the mean
  // rate looks fine while every analysis window has a hole in it.
  if (node.duty < 0.5) return { text: "intermittent", className: "bad" };
  if (node.max_gap_s > 1) return { text: "stalling", className: "warn" };
  if (node.rate_hz < 20) return { text: "starved", className: "warn" };
  if (node.rate_hz < 40) return { text: "thin", className: "warn" };
  return { text: "steady", className: "good" };
}

function label(source: TrafficSource): string {
  if (source.ap) return source.ssid ? `AP · ${source.ssid}` : "AP";
  return "station";
}

function rssiText(source: TrafficSource): string {
  if (source.rssi_mean === null) return "—";
  const spread =
    source.rssi_min !== null && source.rssi_max !== null && source.rssi_min !== source.rssi_max
      ? ` (${source.rssi_min}…${source.rssi_max})`
      : "";
  return `${source.rssi_mean.toFixed(0)} dBm${spread}`;
}

export function trafficView(): View {
  const summary = el("div", { class: "stat-row" });
  const verdictPill = el("span", { class: "pill" }, "—");
  const canvas = el("canvas", { class: "plot-canvas traffic-canvas" });
  const caption = el("p", { class: "panel-note" });
  const sourceList = el("div", { class: "traffic-sources" });
  const sourceHead = el("div", { class: "wifi-subhead" }, el("h3", {}, "Transmitters"));
  const notes = el("div", { class: "traffic-notes" });

  let current: TrafficNode | null = null;
  let pollTimer: number | null = null;
  let showAll = false;

  // Colour per MAC, assigned in the order sources are first seen and kept for as long as the view
  // is on one node. Colouring by rank instead would swap two sources' colours whenever their
  // frame counts crossed, which on a busy channel is every few seconds.
  const colors = new Map<string, string>();
  let coloredNode: number | null = null;

  function colorFor(mac: string): string {
    const existing = colors.get(mac);
    if (existing !== undefined) return existing;
    const next = colors.size < SERIES_COLORS.length ? SERIES_COLORS[colors.size] : REST_COLOR;
    colors.set(mac, next);
    return next;
  }

  function render(nodes: TrafficNode[]) {
    const nodeId = store.selectedNode.value;
    const node = nodes.find((candidate) => candidate.node_id === nodeId) ?? null;

    if (node !== null && node.node_id !== coloredNode) {
      // A different node hears a different set of transmitters; carrying the assignment across
      // would hand the new node's loudest source whatever colour the old one's had.
      colors.clear();
      coloredNode = node.node_id;
      showAll = false;
    }
    current = node;

    renderSummary(node);
    renderSources(node);
    renderNotes(node);
    draw();
  }

  function renderSummary(node: TrafficNode | null) {
    clear(summary);
    if (node === null) {
      verdictPill.className = "pill";
      verdictPill.textContent = "no frames";
      summary.append(
        el(
          "p",
          { class: "hint" },
          "Nothing has arrived from this node. The traffic breakdown is built from the frames " +
            "themselves, so it appears as soon as the first one does.",
        ),
      );
      return;
    }

    const state = verdict(node);
    verdictPill.className = `pill pill-${state.className}`;
    verdictPill.textContent = state.text;

    const stations = node.sources.filter((source) => !source.ap).length;
    const aps = node.sources.length - stations;

    summary.append(
      stat("capture rate", `${node.rate_hz.toFixed(1)} Hz`, node.rate_hz < 40 ? "warn" : ""),
      // Rounded down rather than to nearest: 99.6% displayed as 100% on a link that dropped a
      // second is the one rounding error that matters here.
      stat("duty", `${Math.floor(node.duty * 100)}%`, node.duty < 0.95 ? "warn" : ""),
      stat(
        "longest gap",
        node.max_gap_s >= 0.05 ? `${node.max_gap_s.toFixed(2)} s` : "—",
        node.max_gap_s > 1 ? "bad" : node.max_gap_s > 0.25 ? "warn" : "",
      ),
      stat("transmitters", `${node.sources.length}`),
      stat("access points", `${aps}`),
      stat("frames", node.frames_window.toLocaleString()),
    );

    caption.textContent =
      `frames per second over the last ${node.window_s} s, stacked by transmitter · ` +
      `${node.silent_s} s of the ${node.covered_s} s measured carried nothing`;
  }

  function renderSources(node: TrafficNode | null) {
    clear(sourceList);
    if (node === null) return;

    if (node.sources.length === 0) {
      sourceList.append(
        el(
          "p",
          { class: "hint" },
          node.anonymous > 0
            ? "Every frame from this node arrived without a source address, which is what a v1 " +
              "uplink looks like — the field was added in v2. The rate and duty above are still " +
              "measured; only the attribution is missing."
            : "No transmitters yet.",
        ),
      );
      return;
    }

    const peak = Math.max(1, ...node.counts);
    const shown = showAll ? node.sources : node.sources.slice(0, VISIBLE_SOURCES);
    for (const source of shown) sourceList.append(renderSource(source, peak));

    if (node.sources.length > shown.length) {
      sourceList.append(
        el(
          "button",
          {
            class: "button button-wide",
            onclick: () => {
              showAll = true;
              renderSources(current);
            },
          },
          `Show all ${node.sources.length}`,
        ),
      );
    }
  }

  function renderSource(source: TrafficSource, peak: number): HTMLElement {
    const color = colorFor(source.mac);
    const ratio = window.devicePixelRatio || 1;
    const spark = el("canvas", {
      class: "traffic-spark",
      width: Math.round(SPARK_W * ratio),
      height: Math.round(SPARK_H * ratio),
      style: `width:${SPARK_W}px;height:${SPARK_H}px`,
    }) as HTMLCanvasElement;
    drawSparkline(spark, source.counts, peak, color);

    return el(
      "div",
      { class: "traffic-source" },
      el("span", { class: "traffic-swatch", style: `background:${color}` }),
      el(
        "div",
        { class: "traffic-source-body" },
        el(
          "div",
          { class: "traffic-source-head" },
          el("span", { class: "traffic-mac" }, source.mac),
          el("span", { class: `pill ${source.ap ? "pill-good" : ""}` }, label(source)),
          source.randomized
            ? el(
                "span",
                {
                  class: "pill pill-warn",
                  title:
                    "Locally administered address: a randomized MAC. It will be a different " +
                    "address next time this device associates.",
                },
                "randomized",
              )
            : null,
          source.malformed
            ? el(
                "span",
                {
                  class: "pill pill-bad",
                  title:
                    "A group bit in a source address, which cannot happen on the air. The " +
                    "node is reading the MAC from the wrong offset.",
                },
                "malformed",
              )
            : null,
        ),
        el(
          "div",
          { class: "traffic-stats" },
          el("span", { class: "traffic-rate" }, `${source.rate_hz.toFixed(1)} Hz`),
          el("span", {}, `${Math.round(source.share * 100)}% of frames`),
          el("span", {}, rssiText(source)),
          el("span", { class: "traffic-link" }, `${source.n_sub} sub · ch ${source.channel || "—"}`),
          el(
            "span",
            { class: source.max_gap_s > 1 ? "warn" : "" },
            source.max_gap_s >= 0.05 ? `gap ${source.max_gap_s.toFixed(2)} s` : "no gaps",
          ),
          el("span", { class: "traffic-ago" }, formatAgo(source.last_seen)),
        ),
        el(
          "div",
          { class: "traffic-bar" },
          el("span", {
            style: `width:${Math.max(1, source.share * 100)}%;background:${color}`,
          }),
        ),
      ),
      spark,
    );
  }

  function renderNotes(node: TrafficNode | null) {
    clear(notes);
    if (node === null) return;

    const lines: (Node | string)[] = [];

    if (node.sources.length === 1 && node.anonymous === 0) {
      lines.push(
        "One transmitter only. Either the channel is quiet, or this node is filtered to a " +
          "single BSSID (CSI_AP_MAC), in which case the capture rate is that one access " +
          "point's frame rate and nothing else on the channel can lift it.",
      );
    }
    if (node.anonymous > 0 && node.sources.length > 0) {
      lines.push(
        `${node.anonymous.toLocaleString()} frames carried no source address (a v1 uplink) and ` +
          "are counted in the totals but attributed to nobody.",
      );
    }
    if (node.evicted > 0) {
      lines.push(
        `${node.evicted.toLocaleString()} quiet transmitters have been forgotten to keep the ` +
          "list bounded — an unfiltered monitor hears a long tail of one-off addresses.",
      );
    }
    if (node.rate_hz < 40 && node.frames_window > 0) {
      lines.push(
        el(
          "span",
          {},
          "Below about 40 Hz the heart-rate band is the first thing to go. Turning on traffic " +
            "generation in ",
          el("a", { href: "#wifi" }, "WiFi"),
          " makes the node provoke frames of its own instead of waiting for the household's.",
        ),
      );
    }

    for (const line of lines) notes.append(el("p", { class: "hint" }, line));
  }

  /**
   * The stacked timeline: one column per second, each split by transmitter.
   *
   * Stacked rather than one line per source because the quantity being explained is the total —
   * how many rows a second the waterfall got — and the interesting question is which transmitter
   * the total is made of, and what disappears when it dips.
   */
  function draw() {
    fitCanvas(canvas);
    const plot = beginPlot(canvas);
    if (plot === null) return;

    const node = current;
    if (node === null || node.counts.length === 0) {
      drawFrame(plot, [], []);
      return;
    }

    const area = plotArea(plot);
    const { context } = plot;
    const n = node.counts.length;
    const peak = Math.max(1, ...node.counts);
    const columnWidth = area.w / n;

    // The stretch before this node said anything is not a stall — it is a period we were not
    // listening in — so it is shaded rather than left as an indistinguishable run of zeroes.
    const listeningFrom = n - node.covered_s;
    if (listeningFrom > 0) {
      context.fillStyle = "rgba(255,255,255,0.03)";
      context.fillRect(area.x0, area.y0, listeningFrom * columnWidth, area.h);
    }

    const stacked = new Float64Array(n);
    for (const source of node.sources) {
      context.fillStyle = colorFor(source.mac);
      for (let i = 0; i < n; i++) {
        const count = source.counts[i];
        if (!count) continue;
        const height = (count / peak) * area.h;
        const y = area.y1 - ((stacked[i] / peak) * area.h + height);
        context.fillRect(area.x0 + i * columnWidth, y, Math.max(1, columnWidth - 0.5), height);
        stacked[i] += count;
      }
    }

    // Whatever the sources did not account for: frames with no source address. Drawn last, on
    // top, so a v1 node still gets a timeline instead of an empty plot.
    context.fillStyle = REST_COLOR;
    for (let i = 0; i < n; i++) {
      const remainder = node.counts[i] - stacked[i];
      if (remainder <= 0) continue;
      const height = (remainder / peak) * area.h;
      const y = area.y1 - ((stacked[i] / peak) * area.h + height);
      context.fillRect(area.x0 + i * columnWidth, y, Math.max(1, columnWidth - 0.5), height);
    }

    drawFrame(
      plot,
      [
        [0, `-${node.window_s}s`],
        [0.5, `-${Math.round(node.window_s / 2)}s`],
        [1, "now"],
      ],
      [
        [0, "0"],
        [0.5, String(Math.round(peak / 2))],
        [1, `${peak} Hz`],
      ],
    );
  }

  function drawSparkline(
    spark: HTMLCanvasElement,
    counts: number[],
    peak: number,
    color: string,
  ) {
    const context = spark.getContext("2d");
    if (context === null) return;
    // Normalized to the node's busiest second, not to this source's own, so the rows are
    // comparable: a quiet transmitter should look quiet next to the one carrying the capture.
    const columnWidth = spark.width / counts.length;
    context.fillStyle = color;
    for (let i = 0; i < counts.length; i++) {
      if (!counts[i]) continue;
      // Two device pixels minimum. A second that carried a couple of frames is a whole column
      // here, and rounded down to a sub-pixel sliver it disappears entirely — which would make
      // a source that is quietly still there look like one that stopped.
      const height = Math.max(2, (counts[i] / peak) * spark.height);
      context.fillRect(i * columnWidth, spark.height - height, Math.max(1, columnWidth), height);
    }
  }

  const root = viewLayout({
    className: "view-traffic",
    main: [
      el(
        "div",
        { class: "panel" },
        el(
          "div",
          { class: "panel-head" },
          el("h2", {}, "Capture"),
          verdictPill,
        ),
        summary,
        el("div", { class: "plot-frame traffic-plot" }, canvas),
        caption,
        hintBlock(
          "Every row of the waterfall is one frame that was on the air, so this is the sample " +
            "rate every other view inherits. Rate and duty are different faults: 40 Hz evenly " +
            "spread resamples cleanly, while 40 Hz in bursts leaves holes that the DSP layer " +
            "refuses to interpolate across — which is why breathing can go quiet on a link " +
            "whose average rate looks perfectly healthy.",
        ),
      ),
      el("div", { class: "panel panel-grow" }, sourceHead, sourceList, notes),
    ],
  });

  const unsubscribes: (() => void)[] = [];
  const onResize = () => draw();

  return {
    id: "traffic",
    title: "Traffic",
    root,
    mount() {
      unsubscribes.push(store.traffic.subscribe(render));
      unsubscribes.push(store.selectedNode.subscribe(() => render(store.traffic.value), false));
      window.addEventListener("resize", onResize);
      void store.refreshTraffic();
      pollTimer = window.setInterval(() => void store.refreshTraffic(), POLL_MS);
    },
    unmount() {
      while (unsubscribes.length > 0) unsubscribes.pop()?.();
      window.removeEventListener("resize", onResize);
      if (pollTimer !== null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    },
  };
}
