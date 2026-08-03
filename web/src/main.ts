import "./styles.css";

import { ICONS, clear, closeAllSheets, el, icon, sheet } from "./lib/dom";
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
import { wifiView } from "./views/wifi";

const views: View[] = [
  waterfallView(),
  subcarrierView(),
  motionView(),
  vitalsView("breathing"),
  vitalsView("heart"),
  placementView(),
  sessionsView(),
  healthView(),
  wifiView(),
];

/**
 * The phone's five destinations.
 *
 * All eight views stay reachable and keep their own routes — the desktop sidebar still lists
 * every one. This is only about what a thumb can reach without a menu. The previous bar held all
 * eight and scrolled horizontally, which meant three of them sat off-screen; the selected tab had
 * to be scrolled into view programmatically just to prove that a tap had done anything.
 *
 * `covers` is what makes five tabs enough: a tab stays lit for any view it stands in for, so
 * Vitals reads as selected on both bands and More reads as selected on everything behind it.
 */
interface Tab {
  label: string;
  glyph: string;
  route: string | null;
  covers: string[];
}

const TABS: Tab[] = [
  { label: "Live", glyph: ICONS.waterfall, route: "waterfall", covers: ["waterfall"] },
  { label: "Motion", glyph: ICONS.motion, route: "motion", covers: ["motion"] },
  { label: "Vitals", glyph: ICONS.vitals, route: "breathing", covers: ["breathing", "heart"] },
  { label: "Placement", glyph: ICONS.placement, route: "placement", covers: ["placement"] },
  {
    label: "More",
    glyph: ICONS.more,
    route: null,
    covers: ["subcarriers", "sessions", "health", "wifi"],
  },
];

const MORE: { id: string; blurb: string }[] = [
  { id: "subcarriers", blurb: "A handful of traces, close up — the shape of a single breath" },
  { id: "sessions", blurb: "Record the room, replay it through the same pipeline" },
  { id: "health", blurb: "Rate, jitter, loss and roaming, per node" },
  { id: "wifi", blurb: "Channel and traffic per node; scan the air to pick an access point" },
];

const content = el("main", { class: "content" });
const nav = el("nav", { class: "nav", "aria-label": "Views" });
const tabbar = el("nav", { class: "tabbar", role: "tablist", "aria-label": "Views" });
const nodeSelect = el("select", {
  class: "node-select",
  "aria-label": "Node",
  onchange: (event: Event) => {
    const value = (event.target as HTMLSelectElement).value;
    store.selectNode(value === "" ? null : Number(value));
  },
});
const connectionDot = el("span", { class: "dot dot-bad" });
// Classed so the phone layout can drop the word and keep the dot, which carries the same
// information in a fifth of the width.
const connectionLabel = el("span", { class: "connection-text" }, "connecting");
const rateLabel = el("span", { class: "header-metric" }, "");
const sourceLabel = el("span", { class: "pill header-source" }, "");

let active: View | null = null;

function show(id: string) {
  const view = views.find((candidate) => candidate.id === id);
  if (view === undefined || view === active) return;

  // A sheet belongs to the view under it. Leaving one open across a navigation would strand the
  // scroll lock with nothing on screen to explain it.
  closeAllSheets();

  active?.unmount();
  clear(content);
  content.append(view.root);
  view.mount();
  active = view;

  syncNav(id);

  // The hash is the whole router. Views are cheap to construct and the app has no other state
  // worth encoding, so anything more would be machinery for its own sake.
  if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
}

function syncNav(id: string) {
  for (const button of nav.querySelectorAll("button")) {
    const isActive = button.dataset.view === id;
    button.classList.toggle("nav-active", isActive);
    if (isActive) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const button of tabbar.querySelectorAll("button")) {
    const isActive = (button.dataset.covers ?? "").split(" ").includes(id);
    button.classList.toggle("tab-active", isActive);
    button.setAttribute("aria-selected", isActive ? "true" : "false");
  }
}

for (const view of views) {
  const button = el("button", { class: "nav-button", onclick: () => show(view.id) }, view.title);
  button.dataset.view = view.id;
  nav.append(button);
}

const moreList = el("div", { class: "more-list" });
const moreSheet = sheet({ title: "More views", body: moreList, className: "sheet-more" });

function openMore() {
  clear(moreList);
  for (const entry of MORE) {
    const view = views.find((candidate) => candidate.id === entry.id);
    if (view === undefined) continue;
    moreList.append(
      el(
        "button",
        {
          class: `more-item ${active?.id === entry.id ? "more-item-active" : ""}`,
          type: "button",
          onclick: () => {
            moreSheet.close();
            show(entry.id);
          },
        },
        el("strong", {}, view.title),
        el("span", {}, entry.blurb),
      ),
    );
  }
  moreSheet.open();
}

for (const tab of TABS) {
  const button = el(
    "button",
    {
      class: "tab",
      type: "button",
      role: "tab",
      "aria-selected": "false",
      onclick: () => (tab.route === null ? openMore() : show(tab.route)),
    },
    icon(tab.glyph),
    el("span", { class: "tab-label" }, tab.label),
  );
  button.dataset.covers = tab.covers.join(" ");
  tabbar.append(button);
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
    sourceLabel.className = "pill header-source header-source-replay";
    return;
  }
  if (recording !== null) {
    sourceLabel.textContent = `recording ${recording.label}`;
    sourceLabel.className = "pill header-source header-source-recording";
    return;
  }
  sourceLabel.textContent = "live";
  sourceLabel.className = "pill header-source";
}

const header = el(
  "header",
  { class: "header" },
  el(
    "div",
    { class: "brand" },
    el("span", { class: "brand-mark" }, "CSI"),
    el("span", { class: "brand-text" }, "WiFi sensing"),
  ),
  el("div", { class: "spacer" }),
  el(
    "div",
    { class: "header-status" },
    sourceLabel,
    rateLabel,
    nodeSelect,
    el(
      "div",
      { class: "connection", role: "status", "aria-live": "polite" },
      connectionDot,
      connectionLabel,
    ),
  ),
);

document.getElementById("app")?.append(header, el("div", { class: "layout" }, nav, content, tabbar));

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
