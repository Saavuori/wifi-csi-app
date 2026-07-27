import "./styles.css";

import { clear, el } from "./lib/dom";
import type { NodeHealth, ReplayState, Session } from "./lib/messages";
import { store } from "./lib/store";
import { vitalsView } from "./views/breathing";
import { healthView } from "./views/health";
import { motionView } from "./views/motion";
import { placementView } from "./views/placement";
import { sessionsView } from "./views/sessions";
import { subcarrierView } from "./views/subcarriers";
import type { View } from "./views/view";
import { waterfallView } from "./views/waterfall";

const views: View[] = [
  waterfallView(),
  subcarrierView(),
  motionView(),
  vitalsView("breathing"),
  vitalsView("heart"),
  placementView(),
  sessionsView(),
  healthView(),
];

const content = el("main", { class: "content" });
const nav = el("nav", { class: "nav" });
const nodeSelect = el("select", {
  class: "node-select",
  onchange: (event: Event) => {
    const value = (event.target as HTMLSelectElement).value;
    store.selectNode(value === "" ? null : Number(value));
  },
});
const connectionDot = el("span", { class: "dot dot-bad" });
const connectionLabel = el("span", {}, "connecting");
const rateLabel = el("span", { class: "header-metric" }, "");
const sourceLabel = el("span", { class: "header-source" }, "");

let active: View | null = null;

function show(id: string) {
  const view = views.find((candidate) => candidate.id === id);
  if (view === undefined || view === active) return;

  active?.unmount();
  clear(content);
  content.append(view.root);
  view.mount();
  active = view;

  for (const button of nav.querySelectorAll("button")) {
    button.classList.toggle("nav-active", button.dataset.view === id);
  }
  // The hash is the whole router. Views are cheap to construct and the app has no other state
  // worth encoding, so anything more would be machinery for its own sake.
  if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
}

for (const view of views) {
  const button = el("button", { class: "nav-button", onclick: () => show(view.id) }, view.title);
  button.dataset.view = view.id;
  nav.append(button);
}

function renderNodes(nodes: NodeHealth[]) {
  const selected = store.selectedNode.value;
  clear(nodeSelect);
  if (nodes.length === 0) {
    nodeSelect.append(el("option", { value: "" }, "no nodes"));
    return;
  }
  for (const node of nodes) {
    const option = el(
      "option",
      { value: String(node.node_id) },
      `Node ${node.node_id}${node.online ? "" : " (offline)"}`,
    );
    if (node.node_id === selected) option.selected = true;
    nodeSelect.append(option);
  }
}

function renderSource(recording: Session | null, replay: ReplayState | null) {
  if (replay !== null && !replay.finished) {
    sourceLabel.textContent = `replaying ${replay.path}${replay.playing ? "" : " (paused)"}`;
    sourceLabel.className = "header-source header-source-replay";
    return;
  }
  if (recording !== null) {
    sourceLabel.textContent = `recording ${recording.label}`;
    sourceLabel.className = "header-source header-source-recording";
    return;
  }
  sourceLabel.textContent = "live";
  sourceLabel.className = "header-source";
}

const header = el(
  "header",
  { class: "header" },
  el("div", { class: "brand" }, el("span", { class: "brand-mark" }, "CSI"), "WiFi sensing"),
  el("div", { class: "spacer" }),
  sourceLabel,
  rateLabel,
  nodeSelect,
  el("div", { class: "connection" }, connectionDot, connectionLabel),
);

document.getElementById("app")?.append(header, el("div", { class: "layout" }, nav, content));

store.connected.subscribe((connected) => {
  connectionDot.className = `dot dot-${connected ? "good" : "bad"}`;
  connectionLabel.textContent = connected ? "connected" : "reconnecting";
});
store.nodes.subscribe(renderNodes);
store.selectedNode.subscribe(() => renderNodes(store.nodes.value), false);
store.rate.subscribe((rate) => {
  rateLabel.textContent = rate > 0 ? `${rate} fps` : "";
});
store.recording.subscribe(() => renderSource(store.recording.value, store.replay.value));
store.replay.subscribe(() => renderSource(store.recording.value, store.replay.value));

window.addEventListener("hashchange", () => show(location.hash.slice(1) || views[0].id));
show(location.hash.slice(1) || views[0].id);
