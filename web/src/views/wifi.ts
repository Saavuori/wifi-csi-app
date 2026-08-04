// The WiFi overview: what each node can be told to do, what it has actually done, and what it
// can see on the air. This is the view you open before any of the analysis views, because the
// choice it helps you make — which access point, which channel, and whether to generate traffic
// — decides everything the others measure.
//
// A node earns controls here by reporting back (`applied` non-null): a Pi running the current
// node software takes channel and stimulus changes and can scan. Anything that never reports —
// an ESP32, a replayed recording — is shown read-only, with only the transmitters it was heard
// emitting. The distinction is drawn from the data, never assumed.

import { clear, el, segmented, viewLayout } from "../lib/dom";
import type { ScanAp, Transmitter, WifiNode } from "../lib/messages";
import { store } from "../lib/store";
import type { View } from "./view";

// 2.4 GHz gives 64 subcarriers; 5 GHz on an 80 MHz channel is where the full 256 live. Naming
// the band next to the channel is what makes "point it at 5 GHz for the full width" actionable.
function band(channel: number): string {
  if (channel <= 0) return "";
  return channel <= 14 ? "2.4 GHz" : channel < 32 ? "6 GHz" : "5 GHz";
}

function signalClass(dbm: number | null): string {
  if (dbm === null) return "";
  if (dbm >= -60) return "good";
  if (dbm >= -75) return "warn";
  return "bad";
}

function agoText(ts: number | null): string {
  if (ts === null) return "never";
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  if (seconds < 2) return "just now";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export function wifiView(): View {
  const list = el("div", { class: "wifi-list" });
  let pollTimer: number | null = null;

  function render(nodes: WifiNode[]) {
    clear(list);

    if (nodes.length === 0) {
      list.append(
        el(
          "p",
          { class: "hint" },
          "No nodes yet. A Raspberry Pi node running the current software appears here as soon " +
            "as it polls the server, whether or not it is capturing.",
        ),
      );
      return;
    }

    for (const node of nodes) {
      list.append(renderNode(node));
    }
  }

  function renderNode(node: WifiNode): HTMLElement {
    const controllable = node.applied !== null;
    const pending = controllable && node.reported_rev !== node.revision;

    return el(
      "div",
      { class: "panel wifi-node" },
      el(
        "div",
        { class: "panel-head" },
        el("h2", {}, `Node ${node.node_id}`),
        controllable
          ? el(
              "span",
              { class: `pill pill-${pending ? "warn" : "good"}` },
              pending ? "applying…" : "in sync",
            )
          : el("span", { class: "pill" }, "read-only"),
      ),
      controllable ? renderControls(node, pending) : renderReadOnly(),
      renderScan(node),
      renderTransmitters(node.transmitters),
    );
  }

  function renderControls(node: WifiNode, pending: boolean): HTMLElement {
    const box = el("div", { class: "wifi-controls" });

    // Channel. "auto" means follow the association; an explicit CH/BW retunes the extractor and
    // holds it there. The text field takes the same CH/BW the server validates, so a value from
    // a scan row ("set channel") and a hand-typed one travel the identical path.
    const channelInput = el("input", {
      type: "text",
      class: "wifi-channel-input",
      value: node.desired.channel,
      placeholder: "auto or 36/80",
      "aria-label": "Channel",
    }) as HTMLInputElement;

    const applyChannel = async (value: string) => {
      const next = value.trim() || "auto";
      if (next === node.desired.channel) return;
      try {
        await store.patchNodeControl(node.node_id, { channel: next });
      } catch {
        channelInput.value = node.desired.channel; // reject → snap back to what stuck
      }
    };
    channelInput.addEventListener("change", () => void applyChannel(channelInput.value));

    box.append(
      el(
        "div",
        { class: "control" },
        el("span", { class: "control-label" }, "Channel"),
        el(
          "div",
          { class: "wifi-channel-row" },
          channelInput,
          el(
            "button",
            {
              class: "button",
              onclick: () => {
                channelInput.value = "auto";
                void applyChannel("auto");
              },
            },
            "Auto",
          ),
        ),
      ),
    );

    // Stimulus: the "generate traffic vs. only listen" switch the user asked for. Auto lets the
    // node decide from how busy the channel is; always forces the Ethernet broadcast on (and
    // with it 20 MHz / 64 subcarriers); off is pure listening.
    box.append(
      el(
        "div",
        { class: "control" },
        el("span", { class: "control-label" }, "Traffic generation"),
        segmented({
          value: node.desired.stimulus,
          label: "Stimulus mode",
          options: [
            { value: "auto", label: "Auto" },
            { value: "always", label: "Always" },
            { value: "off", label: "Listen only" },
          ],
          onChange: (value) =>
            void store.patchNodeControl(node.node_id, {
              stimulus: value as "auto" | "always" | "off",
            }),
        }),
      ),
    );

    box.append(
      el(
        "p",
        { class: "hint" },
        stimulusHint(node.desired.stimulus),
      ),
    );

    // What the node actually did, next to what it was asked. When they disagree and it is not
    // just mid-apply, this is where you see it.
    const applied = node.applied ?? {};
    box.append(
      el(
        "div",
        { class: "wifi-applied" },
        appliedStat("asked", describeDesired(node)),
        appliedStat(
          "applied",
          pending ? "pending…" : describeApplied(applied),
          pending ? "warn" : "",
        ),
        appliedStat(
          "on channel",
          applied.observed_channel ? String(applied.observed_channel) : "—",
        ),
      ),
    );

    return box;
  }

  function renderReadOnly(): HTMLElement {
    return el(
      "p",
      { class: "hint" },
      "This node does not accept control — it reports frames but not the channel/stimulus loop. " +
        "The transmitters it has heard are below.",
    );
  }

  function renderScan(node: WifiNode): HTMLElement {
    const box = el("div", { class: "wifi-scan" });
    const controllable = node.applied !== null;

    const head = el(
      "div",
      { class: "wifi-subhead" },
      el("h3", {}, "Nearby access points"),
      controllable
        ? el(
            "button",
            {
              class: "button button-small",
              onclick: () => void store.requestScan(node.node_id),
            },
            "Scan",
          )
        : null,
    );
    box.append(head);

    const scan = node.scan;
    if (scan === null || scan.aps.length === 0) {
      box.append(
        el(
          "p",
          { class: "hint" },
          controllable
            ? "No scan yet. Scanning pauses capture for a few seconds while the radio sweeps."
            : "Read-only nodes cannot scan.",
        ),
      );
      return box;
    }

    const table = el("table", { class: "wifi-table" });
    table.append(
      el(
        "tr",
        {},
        el("th", {}, "SSID"),
        el("th", {}, "Ch"),
        el("th", {}, "Signal"),
        el("th", {}, "Load"),
        el("th", {}, ""),
      ),
    );
    for (const ap of scan.aps) {
      table.append(renderApRow(node, ap));
    }
    box.append(
      table,
      el("p", { class: "wifi-scan-age hint" }, `scanned ${agoText(scan.ts)}`),
    );
    return box;
  }

  function renderApRow(node: WifiNode, ap: ScanAp): HTMLElement {
    const controllable = node.applied !== null;
    const spec = ap.channel > 0 ? `${ap.channel}/${ap.width}` : null;
    const isCurrent = node.desired.channel === spec;

    const load =
      ap.utilisation !== null
        ? `${Math.round(ap.utilisation * 100)}%${ap.stations !== null ? ` · ${ap.stations}` : ""}`
        : ap.stations !== null
          ? `${ap.stations} sta`
          : "—";

    return el(
      "tr",
      { class: ap.associated ? "wifi-ap-associated" : "" },
      el(
        "td",
        {},
        el("div", { class: "wifi-ssid" }, ap.ssid || "(hidden)"),
        el("div", { class: "wifi-bssid" }, ap.bssid),
      ),
      el(
        "td",
        {},
        el("div", {}, String(ap.channel || "—")),
        el("div", { class: "wifi-band" }, band(ap.channel)),
      ),
      el(
        "td",
        { class: `wifi-signal ${signalClass(ap.signal)}` },
        ap.signal !== null ? `${Math.round(ap.signal)}` : "—",
        el("span", { class: "wifi-unit" }, ap.signal !== null ? " dBm" : ""),
      ),
      el("td", { class: "wifi-load" }, load),
      el(
        "td",
        { class: "wifi-ap-action" },
        controllable && spec !== null
          ? el(
              "button",
              {
                class: `button button-small ${isCurrent ? "button-primary" : ""}`,
                disabled: isCurrent,
                title: `Tune this node to channel ${spec}`,
                onclick: () => void store.patchNodeControl(node.node_id, { channel: spec }),
              },
              isCurrent ? "current" : "use",
            )
          : null,
      ),
    );
  }

  function renderTransmitters(transmitters: Transmitter[]): HTMLElement {
    const box = el("div", { class: "wifi-tx" });
    box.append(el("h3", {}, "Transmitters heard"));

    if (transmitters.length === 0) {
      box.append(
        el(
          "p",
          { class: "hint" },
          "Nothing yet. These are the source addresses of the frames this node has actually " +
            "captured — the access point it is filtered to, or every station on the channel " +
            "when unfiltered.",
        ),
      );
      return box;
    }

    const table = el("table", { class: "wifi-table" });
    table.append(
      el("tr", {}, el("th", {}, "MAC"), el("th", {}, "Ch"), el("th", {}, "RSSI"), el("th", {}, "Frames")),
    );
    for (const tx of transmitters) {
      table.append(
        el(
          "tr",
          {},
          el("td", { class: "wifi-bssid" }, tx.mac),
          el("td", {}, String(tx.channel || "—")),
          el("td", { class: `wifi-signal ${signalClass(tx.rssi)}` }, `${tx.rssi}`),
          el("td", {}, tx.frames.toLocaleString()),
        ),
      );
    }
    box.append(table);
    return box;
  }

  const root = viewLayout({
    className: "view-wifi",
    main: [
      el(
        "div",
        { class: "panel panel-grow wifi-intro" },
        el("div", { class: "panel-head" }, el("h2", {}, "WiFi")),
        el(
          "p",
          { class: "hint" },
          "Pick the access point and channel each node measures against, and choose whether it " +
            "generates traffic or only listens. 2.4 GHz resolves 64 subcarriers; a 5 GHz " +
            "network on an 80 MHz channel resolves the full 256.",
        ),
      ),
      list,
    ],
  });

  const unsubscribes: (() => void)[] = [];

  return {
    id: "wifi",
    title: "WiFi",
    root,
    mount() {
      unsubscribes.push(store.wifi.subscribe(render));
      void store.refreshWifi();
      // A scan result or a newly-appeared node arrives by WebSocket, but transmitter counts and
      // "x seconds ago" only move on a poll. Slow on purpose — this is a setup screen, not an
      // instrument.
      pollTimer = window.setInterval(() => void store.refreshWifi(), 5000);
    },
    unmount() {
      while (unsubscribes.length > 0) unsubscribes.pop()?.();
      if (pollTimer !== null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    },
  };
}

function stimulusHint(mode: string): string {
  switch (mode) {
    case "always":
      return "Emits Ethernet broadcast the access point floods onto the air, so the capture " +
        "rate holds even on an idle network — at the cost of 20 MHz / 64 subcarriers while on.";
    case "off":
      return "Pure listening: the node measures only the traffic that already exists. Full " +
        "width, but the rate follows the household — a few frames a second on an idle network.";
    default:
      return "The node generates traffic only when the channel goes quiet, and steps back to " +
        "full-width listening whenever there is enough real traffic to measure.";
  }
}

function describeDesired(node: WifiNode): string {
  return `${node.desired.channel} · ${node.desired.stimulus}`;
}

function describeApplied(applied: NonNullable<WifiNode["applied"]>): string {
  const channel = applied.channel ?? "?";
  const stimulus = applied.stimulus ?? "?";
  const width = applied.narrowband ? " (20 MHz)" : "";
  return `${channel} · ${stimulus}${width}`;
}

function appliedStat(label: string, value: string, className = ""): HTMLElement {
  return el(
    "div",
    { class: `wifi-applied-stat ${className}` },
    el("span", { class: "wifi-applied-value" }, value),
    el("span", { class: "wifi-applied-label" }, label),
  );
}
