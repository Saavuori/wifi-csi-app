// Phase 7 on screen: teach the room its zones, then watch it name them.
//
// This is the most phone-shaped view in the app, for the same reason the placement tuner is —
// the whole activity happens while you are walking around the house holding the instrument. So
// the record flow is a big target, a countdown you can read from across a room, and a result you
// can act on without going back to a desk.
//
// The other half is the part that makes re-recording worth doing. Every example carries its
// leave-one-out verdict, so "this one gets classified as hallway" is visible on the example
// itself rather than being a vague sense that the feature is not working. That is what turns
// deleting an example into a decision instead of a guess — and when two zones are genuinely
// indistinguishable down this link, the panel says so, because no amount of extra recording
// will fix geometry.

import { clear, el, formatBytes, formatDuration, hintBlock, slider, viewLayout } from "../lib/dom";
import type { Metrics, ZoneCapture, ZoneReport, ZoneSample, ZoneState } from "../lib/messages";
import { store } from "../lib/store";
import type { View } from "./view";

const STATE_COLOR: Record<string, string> = {
  matched: "#06d6a0",
  ambiguous: "#ffd166",
  unknown: "#f4a261",
  settling: "#9aa5b1",
  idle: "#4cc9f0",
  untrained: "#9aa5b1",
  stale: "#ef476f",
};

const VERDICT_LABEL: Record<string, string> = {
  correct: "matches its zone",
  wrong: "classifies as another zone",
  "only-sample": "the only example of this zone",
  unusable: "no usable windows",
  unscored: "not scored yet",
};

export function zonesView(): View {
  const liveBox = el("div", { class: "panel-body" });
  const badge = el("div", { class: "state-badge" }, "—");
  const recordBox = el("div", { class: "controls" });
  const listBox = el("div", { class: "zone-list" });
  const qualityBox = el("div", { class: "panel-body" });

  let report: ZoneReport | null = null;
  let capture: ZoneCapture | null = null;
  let duration = 20;
  let countdown = 5;
  let error: string | null = null;

  const zoneName = (id: string | null | undefined) =>
    report?.zones.find((zone) => zone.id === id)?.name ?? id ?? "—";

  // -- the live readout ---------------------------------------------------------------------

  function renderLive(zone: ZoneState | undefined) {
    clear(liveBox);

    const state = zone?.state ?? "idle";
    badge.textContent = state === "matched" ? zoneName(zone?.zone_id) : state;
    badge.style.background = STATE_COLOR[state] ?? "#9aa5b1";

    if (zone === undefined) {
      liveBox.append(el("p", { class: "hint" }, "Waiting for the first analysis window."));
      return;
    }
    if (zone.reason) {
      liveBox.append(el("p", { class: "hint" }, zone.reason));
    }

    const scores = zone.scores ?? [];
    if (scores.length === 0) return;

    // Distance bars, worst-case scaled so the winner is always readable. Showing every zone's
    // distance rather than just the answer is what makes a wrong answer diagnosable: two bars
    // of nearly equal length is a different problem from one long bar, and they want different
    // fixes — move the node, or record more examples.
    const worst = Math.max(...scores.map((entry) => entry.distance), 1e-6);
    for (const entry of scores) {
      const share = Math.min(1, entry.distance / worst);
      liveBox.append(
        el(
          "div",
          { class: "zone-score" },
          el("span", { class: "zone-score-name" }, zoneName(entry.zone_id)),
          el(
            "span",
            { class: "zone-score-track" },
            el("span", {
              class: `zone-score-fill ${entry.zone_id === zone.zone_id ? "zone-score-best" : ""}`,
              style: `width: ${(share * 100).toFixed(1)}%`,
            }),
          ),
          el("span", { class: "zone-score-value" }, entry.distance.toFixed(3)),
        ),
      );
    }
  }

  function onMetrics(metrics: Map<number, Metrics>) {
    const nodeId = store.selectedNode.value;
    renderLive(nodeId === null ? undefined : metrics.get(nodeId)?.zone);
  }

  // -- recording an example -----------------------------------------------------------------

  function renderRecord() {
    clear(recordBox);

    if (capture !== null) {
      const total = capture.phase === "countdown" ? capture.countdown_s : capture.duration_s;
      const done = total > 0 ? 1 - capture.remaining_s / total : 1;
      recordBox.append(
        el(
          "div",
          { class: "zone-capture" },
          el(
            "div",
            { class: "zone-capture-head" },
            capture.phase === "countdown"
              ? `Walk to ${capture.zone_name}`
              : capture.phase === "recording"
                ? `Recording ${capture.zone_name} — keep moving`
                : "Saving",
          ),
          el(
            "div",
            { class: "zone-capture-count" },
            capture.phase === "saving" ? "…" : Math.ceil(capture.remaining_s).toFixed(0),
          ),
          el(
            "span",
            { class: "zone-score-track" },
            el("span", {
              class: "zone-score-fill zone-score-best",
              style: `width: ${(Math.max(0, Math.min(1, done)) * 100).toFixed(1)}%`,
            }),
          ),
        ),
        el(
          "button",
          {
            class: "button button-danger button-wide",
            onclick: () => void store.cancelZoneCapture(),
          },
          "Cancel",
        ),
      );
      return;
    }

    const nodeId = store.selectedNode.value;
    const zones = report?.zones ?? [];

    if (error !== null) recordBox.append(el("p", { class: "zone-error" }, error));

    const result = store.zoneResult.value;
    if (result !== null && error === null) {
      recordBox.append(
        el(
          "p",
          { class: result.ok ? "hint" : "zone-error" },
          result.ok
            ? `Saved${result.sample?.quiet ? " — but almost nothing moved during it" : ""}.`
            : (result.error ?? "The capture failed."),
        ),
      );
    }

    if (nodeId === null) {
      recordBox.append(el("p", { class: "hint" }, "Select a node first — a fingerprint belongs to one link."));
      return;
    }
    if (zones.length === 0) {
      recordBox.append(el("p", { class: "hint" }, "Create a zone first, then record examples of moving in it."));
      return;
    }

    recordBox.append(
      el("div", { class: "control-label" }, "Record an example of moving in:"),
      el(
        "div",
        { class: "chip-row" },
        ...zones.map((zone) =>
          el(
            "button",
            {
              class: "chip chip-large",
              onclick: () => {
                void store
                  .recordZoneSample(zone.id, nodeId, {
                    duration_s: duration,
                    countdown_s: countdown,
                  })
                  .then((message) => {
                    error = message;
                    renderRecord();
                  });
              },
            },
            zone.name,
          ),
        ),
      ),
      slider({
        label: "Countdown",
        min: 0,
        max: 30,
        step: 1,
        value: countdown,
        format: (v) => `${v.toFixed(0)}s`,
        onInput: (value) => (countdown = value),
        hint: "Time to walk there before the capture starts",
      }),
      slider({
        label: "Length",
        min: 5,
        max: 60,
        step: 1,
        value: duration,
        format: (v) => `${v.toFixed(0)}s`,
        onInput: (value) => (duration = value),
        hint: "Longer examples yield more analysis windows, so the zone's own spread is better measured",
      }),
    );
  }

  // -- zones and their examples ---------------------------------------------------------------

  /**
   * The access point each node's newest example was recorded against.
   *
   * Anything older that disagrees is from a link the examples can no longer be compared across,
   * and is flagged. Comparing every sample against every other instead would light up all of
   * them, including the current ones, which tells you a link changed but not which examples are
   * now dead weight.
   */
  function currentLinks(): Map<number, string> {
    const links = new Map<number, string>();
    for (const sample of report?.samples ?? []) links.set(sample.node_id, sample.src_mac);
    return links;
  }

  function sampleRow(sample: ZoneSample, links: Map<number, string>) {
    const stale = links.get(sample.node_id) !== sample.src_mac;

    return el(
      "div",
      { class: `zone-sample zone-sample-${sample.verdict}` },
      el(
        "div",
        { class: "zone-sample-main" },
        el(
          "div",
          { class: "zone-sample-meta" },
          `${new Date(sample.recorded_at * 1000).toLocaleString()} · ` +
            `${formatDuration(sample.duration_s)} · node ${sample.node_id} · ` +
            `${sample.n_sub} subcarriers`,
        ),
        el(
          "div",
          { class: "zone-sample-verdict" },
          VERDICT_LABEL[sample.verdict] ?? sample.verdict,
          sample.verdict === "wrong" && sample.predicted
            ? `: ${zoneName(sample.predicted)}`
            : "",
        ),
        sample.quiet
          ? el(
              "div",
              { class: "zone-sample-warn" },
              `Motion during this ran at ${sample.motion_median.toFixed(1)}, below the ` +
                `${sample.motion_enter.toFixed(1)} the presence detector calls "someone is ` +
                "here\". Either little was moving, or the detector's baseline was calibrated " +
                "while the room was not empty.",
            )
          : null,
        stale
          ? el(
              "div",
              { class: "zone-sample-warn" },
              `Recorded against ${sample.src_mac || "an unknown access point"}, which node ` +
                `${sample.node_id} is no longer on. It is kept as a separate model and plays ` +
                "no part in classifying the current link.",
            )
          : null,
      ),
      el(
        "button",
        {
          class: "button button-danger",
          onclick: () => void store.deleteZoneSample(sample.zone_id, sample.id),
        },
        "Delete",
      ),
    );
  }

  function renderZones() {
    clear(listBox);

    const zones = report?.zones ?? [];
    if (zones.length === 0) {
      listBox.append(
        el(
          "p",
          { class: "hint" },
          "No zones yet. Create one for each place you want told apart, then record a few " +
            "examples of moving in each.",
        ),
      );
      return;
    }

    const links = currentLinks();

    for (const zone of zones) {
      const samples = (report?.samples ?? []).filter((sample) => sample.zone_id === zone.id);
      const accuracy = report?.accuracy[zone.id];
      const scored = accuracy?.accuracy;

      const name = el("input", {
        class: "session-label",
        type: "text",
        value: zone.name,
        onchange: (event: Event) =>
          void store.renameZone(zone.id, (event.target as HTMLInputElement).value),
      });

      listBox.append(
        el(
          "div",
          { class: "zone" },
          el(
            "div",
            { class: "zone-head" },
            name,
            el(
              "span",
              { class: "zone-accuracy" },
              scored === null || scored === undefined
                ? `${samples.length} example${samples.length === 1 ? "" : "s"}`
                : `${samples.length} examples · ${(scored * 100).toFixed(0)}% correct`,
            ),
            el(
              "button",
              {
                class: "button button-danger",
                onclick: () => {
                  if (!confirm(`Delete ${zone.name}? Its examples go too.`)) return;
                  void store.deleteZone(zone.id);
                },
              },
              "Delete zone",
            ),
          ),
          samples.length < 3
            ? el(
                "p",
                { class: "hint" },
                samples.length === 0
                  ? "No examples yet."
                  : "Fewer than three examples. The spread of a zone about its own centroid is " +
                    "what its rejection threshold is calibrated from, so until there are a few " +
                    "this zone will mostly read as unknown or ambiguous rather than wrong — " +
                    "which is the safe direction, but not a useful one.",
              )
            : null,
          samples.length === 0
            ? null
            : el(
                "div",
                { class: "zone-samples" },
                ...samples.map((sample) => sampleRow(sample, links)),
              ),
        ),
      );
    }
  }

  // -- how well it can possibly work -----------------------------------------------------------

  function renderQuality() {
    clear(qualityBox);
    if (report === null) return;

    if (report.indistinguishable.length > 0) {
      qualityBox.append(
        el(
          "div",
          { class: "zone-warning" },
          el("strong", {}, "These zones cannot be told apart on this link"),
          ...report.indistinguishable.map((pair) =>
            el("div", {}, pair.zones.map(zoneName).join(" and ")),
          ),
          el(
            "p",
            { class: "hint" },
            "They are confused in both directions, which means they sit at similar geometry " +
              "relative to the line between the node and the access point. More examples will " +
              "not separate them — moving the node, or locking to a different access point, " +
              "might. Otherwise they are honestly one zone.",
          ),
        ),
      );
    }

    const zones = report.zones;
    if (zones.length > 1) {
      // Zone columns, then whichever non-answers actually occurred. Without those a zone whose
      // examples all came back "ambiguous" renders as an empty row, which reads as missing data
      // rather than as the finding it is.
      const outcomes = new Set<string>();
      for (const counts of Object.values(report.confusion)) {
        for (const [predicted, count] of Object.entries(counts)) {
          if (count > 0 && !zones.some((zone) => zone.id === predicted)) outcomes.add(predicted);
        }
      }
      const columns = [
        ...zones.map((zone) => ({ id: zone.id, label: zone.name, isZone: true })),
        ...[...outcomes].sort().map((id) => ({ id, label: id, isZone: false })),
      ];

      const header = el(
        "tr",
        {},
        el("th", {}, "recorded ↓ / read as →"),
        ...columns.map((column) => el("th", {}, column.label)),
      );
      const rows = zones.map((row) =>
        el(
          "tr",
          {},
          el("th", {}, row.name),
          ...columns.map((column) => {
            const count = report?.confusion[row.id]?.[column.id] ?? 0;
            const good = column.isZone && row.id === column.id;
            return el(
              "td",
              { class: count === 0 ? "" : good ? "zone-cell-good" : "zone-cell-bad" },
              count > 0 ? String(count) : "·",
            );
          }),
        ),
      );
      qualityBox.append(
        el(
          "div",
          { class: "zone-matrix-scroll" },
          el("table", { class: "zone-matrix" }, header, ...rows),
        ),
      );
    }

    qualityBox.append(
      el(
        "p",
        { class: "panel-note" },
        `${report.samples.length} examples · ${formatBytes(report.bytes)} of ` +
          `${formatBytes(report.max_bytes)}`,
      ),
    );
  }

  // -- assembly --------------------------------------------------------------------------------

  function renderAll() {
    renderRecord();
    renderZones();
    renderQuality();
  }

  const newZone = el("input", { type: "text", placeholder: "e.g. Kitchen" });
  const config = store.config.value;

  const controls = el(
    "div",
    { class: "controls" },
    el(
      "label",
      { class: "control" },
      el("span", { class: "control-label" }, "New zone"),
      newZone,
    ),
    el(
      "button",
      {
        class: "button button-primary button-wide",
        onclick: () => {
          const name = newZone.value.trim();
          if (!name) return;
          newZone.value = "";
          void store.createZone(name);
        },
      },
      "Create zone",
    ),
    slider({
      label: "Vote window",
      min: 0,
      max: 15,
      step: 0.5,
      value: config?.zones.vote_s ?? 3,
      format: (v) => `${v.toFixed(1)}s`,
      onInput: (value) => store.patchConfig({ zones: { vote_s: value } }),
      hint: "Majority over this long before the displayed zone changes",
    }),
    slider({
      label: "Rejection radius",
      min: 0.1,
      max: 1.5,
      step: 0.05,
      value: config?.zones.reject_separation_frac ?? 0.45,
      format: (v) => `${v.toFixed(2)}× separation`,
      onInput: (value) => store.patchConfig({ zones: { reject_separation_frac: value } }),
      hint: "As a fraction of the distance between zones. Lower means more 'unknown', which is the safer direction",
    }),
    slider({
      label: "Ambiguity margin",
      min: 0,
      max: 0.6,
      step: 0.02,
      value: config?.zones.margin_frac ?? 0.15,
      format: (v) => `${v.toFixed(2)}× separation`,
      onInput: (value) => store.patchConfig({ zones: { margin_frac: value } }),
      hint: "How far ahead the best zone must be before it is named rather than called ambiguous",
    }),
    hintBlock(
      "This recognizes places you have taught it; it does not locate anyone. Every example is " +
        "tied to the link it was recorded on, so moving the node — or the mesh moving it for " +
        "you — invalidates all of them at once, and the readout will say so rather than guess. " +
        "Record each zone from a few different spots within it, and re-record after the " +
        "furniture moves.",
    ),
  );

  const root = viewLayout({
    controlsTitle: "Zones",
    main: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Where the movement is"), badge),
        liveBox,
      ),
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Teach")),
        recordBox,
      ),
      el(
        "div",
        { class: "panel panel-grow" },
        el("div", { class: "panel-head" }, el("h2", {}, "Zones and examples")),
        listBox,
      ),
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "How well they separate")),
        qualityBox,
      ),
    ],
    side: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Zones")),
        controls,
      ),
    ],
  });

  const unsubscribes: (() => void)[] = [];

  return {
    id: "zones",
    title: "Zones",
    root,
    mount() {
      unsubscribes.push(store.metrics.subscribe(onMetrics));
      unsubscribes.push(
        store.zones.subscribe((value) => {
          report = value;
          renderAll();
        }),
      );
      unsubscribes.push(
        store.zoneCapture.subscribe((value) => {
          capture = value;
          if (value !== null) error = null;
          renderRecord();
        }),
      );
      unsubscribes.push(store.zoneResult.subscribe(() => renderRecord(), false));
      unsubscribes.push(store.selectedNode.subscribe(() => renderRecord(), false));
      void store.refreshZones();
    },
    unmount() {
      while (unsubscribes.length > 0) unsubscribes.pop()?.();
    },
  };
}
