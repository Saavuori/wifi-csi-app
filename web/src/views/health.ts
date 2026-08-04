// Node health. These are the numbers Phase 1's exit criteria are written against — a stable
// ~80 Hz, sequence gaps under 1% over ten minutes — so they are checked continuously rather
// than by going and looking at a serial console.
//
// The loss rate here counts device-side drops and in-flight losses together, because the
// sequence number advances on every frame the callback accepted whether or not the ring had
// room for it. That is the number worth watching: it does not matter where a frame was lost.

import { clear, el, formatDuration, hintBlock, select, slider, toggle, viewLayout } from "../lib/dom";
import type { NodeHealth, ServerConfig } from "../lib/messages";
import { store } from "../lib/store";
import type { View } from "./view";

function verdict(node: NodeHealth): { text: string; className: string } {
  if (!node.online) return { text: "offline", className: "bad" };
  if (node.loss_rate > 0.01) return { text: "losing frames", className: "bad" };
  // Ranked above "slow" on purpose. A node that keeps changing access point is not producing a
  // degraded measurement, it is producing a measurement of somewhere else every few minutes,
  // and the fix (lock the BSSID) is different from the fix for a low rate.
  if (node.roams > 0) return { text: "roaming", className: "warn" };
  if (node.rate_hz < 40) return { text: "slow", className: "warn" };
  if (node.jitter_ms > 5) return { text: "jittery", className: "warn" };
  return { text: "healthy", className: "good" };
}

export function healthView(): View {
  const grid = el("div", { class: "node-grid" });
  const preprocessing = el("div", { class: "controls" });

  function renderNodes(nodes: NodeHealth[]) {
    clear(grid);

    if (nodes.length === 0) {
      grid.append(
        el(
          "p",
          { class: "hint" },
          // Deliberately no "run the synthetic node" suggestion. That tool was removed when
          // every runtime path that could emit fabricated frames was taken out, and pointing
          // at a command that does not exist is worse than offering nothing. On a Pi the
          // answer is almost always the node service rather than the server.
          "No nodes have sent anything yet. On a Raspberry Pi, check the node is running: " +
            "systemctl status csi-node, then journalctl -u csi-node -n 40. Otherwise check " +
            "the server's UDP port and the node's CSI_SERVER_HOST.",
        ),
      );
      return;
    }

    for (const node of nodes) {
      const state = verdict(node);
      grid.append(
        el(
          "div",
          { class: `node-card ${state.className}` },
          el(
            "div",
            { class: "node-head" },
            el("span", { class: `dot dot-${state.className}` }),
            el("strong", {}, `Node ${node.node_id}`),
            el("span", { class: `pill pill-${state.className} node-verdict` }, state.text),
          ),
          el(
            "div",
            { class: "node-stats" },
            metric("rate", `${node.rate_hz.toFixed(1)} Hz`, node.rate_hz < 40 ? "warn" : ""),
            metric("jitter", `${node.jitter_ms.toFixed(2)} ms`, node.jitter_ms > 5 ? "warn" : ""),
            metric(
              "loss",
              `${(node.loss_rate * 100).toFixed(2)}%`,
              node.loss_rate > 0.01 ? "bad" : "",
            ),
            metric("gaps", node.gaps.toLocaleString()),
            metric("frames", node.frames.toLocaleString()),
            metric("RSSI", `${node.rssi} dBm`),
            // A dash, not a number, when the driver reports no noise floor. Showing rssi - 0
            // gave a confident "-49 dB" on a healthy link, which is the kind of figure someone
            // tunes a threshold against.
            metric("SNR", node.snr_db === null ? "—" : `${node.snr_db} dB`),
            metric("channel", String(node.channel)),
            metric("subcarriers", String(node.n_sub)),
            metric("uptime", formatDuration(node.uptime_s)),
            metric("reboots", String(node.reboots), node.reboots > 0 ? "warn" : ""),
            metric("reorders", String(node.reorders)),
            metric("roams", String(node.roams), node.roams > 0 ? "warn" : ""),
          ),
          ...(node.src_mac
            ? [
                el(
                  "p",
                  { class: "hint" },
                  `link ${node.src_mac} · epoch ${node.link_epoch}` +
                    (node.roams > 0
                      ? " — this node has changed access point mid-session, which invalidates " +
                        "the calibration each time. Set CSI_LOCK_BSSID in the firmware."
                      : ""),
                ),
              ]
            : []),
          el(
            "button",
            {
              class: `button button-wide ${
                store.selectedNode.value === node.node_id ? "button-primary" : ""
              }`,
              onclick: () => store.selectNode(node.node_id),
            },
            store.selectedNode.value === node.node_id ? "Selected" : "Select",
          ),
        ),
      );
    }
  }

  function metric(label: string, value: string, className = ""): HTMLElement {
    return el(
      "div",
      { class: `node-metric ${className}` },
      el("span", { class: "node-metric-value" }, value),
      el("span", { class: "node-metric-label" }, label),
    );
  }

  function renderPreprocessing(config: ServerConfig | null) {
    clear(preprocessing);
    if (config === null) return;

    preprocessing.append(
      select({
        label: "AGC normalization",
        value: config.preprocess.norm_mode,
        options: [
          { value: "hybrid", label: "Hybrid (RMS + slow RSSI trend)" },
          { value: "rssi", label: "RSSI per frame" },
          { value: "rms", label: "Per-frame RMS only" },
          { value: "none", label: "None (raw)" },
        ],
        onChange: (value) => store.patchConfig({ preprocess: { norm_mode: value } }),
        hint:
          "Hybrid divides out the measured RMS — exact gain removal, no quantization — and " +
          "re-applies a slow RSSI trend so genuine power changes survive. Per-frame RSSI is " +
          "faithful to real changes but injects a ~12% step on every 1 dB RSSI tick.",
      }),
      toggle({
        label: "Hampel filter",
        value: config.preprocess.hampel_enabled,
        onChange: (value) => store.patchConfig({ preprocess: { hampel_enabled: value } }),
        hint: "Replaces isolated per-subcarrier spikes with the local median",
      }),
      toggle({
        label: "Drop pilot subcarriers",
        value: config.preprocess.drop_pilots,
        onChange: (value) => store.patchConfig({ preprocess: { drop_pilots: value } }),
        hint: "Pilots are modulated by a known sign sequence, which reads as flutter",
      }),
      toggle({
        label: "Drop DC-adjacent subcarriers",
        value: config.preprocess.drop_dc_adjacent,
        onChange: (value) => store.patchConfig({ preprocess: { drop_dc_adjacent: value } }),
        hint:
          "Turn on for a Raspberry Pi node. The BCM43455 leaks its DC offset into k = ±1 — " +
          "measured at 8x the median amplitude in every frame — and subcarrier selection " +
          "ranks by variance, so the bin carrying no signal is a strong candidate to be picked.",
      }),
      slider({
        label: "AGC step threshold",
        min: 0.2,
        max: 6,
        step: 0.1,
        value: config.preprocess.agc_step_db,
        format: (v) => `${v.toFixed(1)} dB`,
        onInput: (value) => store.patchConfig({ preprocess: { agc_step_db: value } }),
        hint: "How large a uniform jump has to be before it counts as a gain event",
      }),
      slider({
        label: "AGC uniformity",
        min: 0.05,
        max: 1.5,
        step: 0.05,
        value: config.preprocess.agc_uniformity,
        format: (v) => v.toFixed(2),
        onInput: (value) => store.patchConfig({ preprocess: { agc_uniformity: value } }),
        hint:
          "Lower is stricter. A gain step scales every subcarrier by nearly the same factor; " +
          "real motion is frequency-selective and spreads out.",
      }),
    );
  }

  const root = viewLayout({
    controlsTitle: "Preprocessing",
    main: [
      el(
        "div",
        { class: "panel panel-grow" },
        el("div", { class: "panel-head" }, el("h2", {}, "Nodes")),
        grid,
      ),
    ],
    side: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Preprocessing")),
        preprocessing,
        hintBlock(
          "These apply identically to live and replayed frames — that is what makes a " +
            "recording a faithful stand-in for the room.",
        ),
      ),
    ],
  });

  const unsubscribes: (() => void)[] = [];

  return {
    id: "health",
    title: "Node health",
    root,
    mount() {
      unsubscribes.push(store.nodes.subscribe(renderNodes));
      unsubscribes.push(store.config.subscribe(renderPreprocessing));
      unsubscribes.push(store.selectedNode.subscribe(() => renderNodes(store.nodes.value), false));
    },
    unmount() {
      while (unsubscribes.length > 0) unsubscribes.pop()?.();
    },
  };
}
