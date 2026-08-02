// Which radio the instrument measures, and how it is made to transmit.
//
// CSI measures the path between a transmitter and whatever it is transmitting to, so on a mesh
// the choice of radio *is* the choice of what the instrument looks at. The nearest unit is
// usually the worst one to pick: a mesh node sitting in the same room as the Pi, with nothing
// moving in the metre between them, gives a beautifully clean and perfectly static channel. The
// radio worth measuring is the one whose path crosses the space you care about, and it is
// frequently not the loudest. That is the first reason this view exists.
//
// The second is that a chosen radio measured in silence gives nothing. The extractor produces
// CSI only for HT/VHT frames, which means only for frames an access point sends *to a station* —
// so the instrument's floor is however much traffic the household happens to be putting through
// that access point, which at four in the morning is close to zero. The probe settings here are
// what buys a floor, and the measured yield is the only honest answer to whether they are.
//
// Nothing on this page can cost us the machine. Every choice here moves the *monitor* radio,
// which is not the interface the node is reached over — that is either a USB dongle's
// association or eth0 — and a selection never touches it.

import { clear, el, formatDuration, viewLayout } from "../lib/dom";
import type { AccessPoint, ApProbe, ApSelectRequest, ApStatus, ProbeMode } from "../lib/messages";
import { store } from "../lib/store";
import type { View } from "./view";

// The published list is regenerated on the host every ~30 s, so polling much faster than this
// only re-reads the same file. Fast enough that a selection's effect shows up promptly.
const REFRESH_MS = 10_000;

// Mirrors the server's ceiling. Same reasoning: capture is capped at 100 Hz and the yield curve
// has already flattened by 300 Hz sent, so this is a guard against a slipped decimal point
// rather than a tuning parameter.
const PROBE_HZ_MAX = 1000;

const PROBE_MODES: { value: ProbeMode; label: string }[] = [
  { value: "broadcast", label: "Broadcast — the AP's acknowledgements" },
  { value: "unicast", label: "Unicast — the AP's delivery to a client" },
  { value: "icmp", label: "ICMP echo — the replies" },
];

// Everything a selection can say about probing, and nothing it can say about which radio to
// measure: the two are decided in different places here, and this half must never overwrite the
// other by being spread over it.
type ProbeFields = Pick<ApSelectRequest, "probe_host" | "probe_mode" | "probe_hz">;

function sameMac(a: string | undefined, b: string | undefined): boolean {
  if (!a || !b) return false;
  return a.toLowerCase() === b.toLowerCase();
}

function formatHz(hz: number): string {
  if (!Number.isFinite(hz)) return "—";
  return hz >= 10 ? `${Math.round(hz)}` : `${hz.toFixed(1)}`;
}

/**
 * How much confidence the measured yield deserves.
 *
 * The measured numbers on this hardware are unambiguous at both ends: probing that works buys
 * tens of Hz (100 Hz of broadcast bought 78), and probing that does not buys nothing at all.
 * The middle is left uncoloured because it genuinely is ambiguous — a few Hz could as easily be
 * the household's own traffic drifting as the probes landing.
 */
function yieldClass(inducedHz: number): string {
  if (inducedHz >= 5) return "hint-good";
  if (inducedHz < 1) return "hint-bad";
  return "";
}

export function apsView(): View {
  const list = el("div", { class: "ap-list" });
  const summary = el("div", { class: "ap-summary" });
  const outcome = el("p", { class: "hint" });
  const probeNotes = el("div", { class: "ap-notes" });
  const routeNote = el("p", { class: "hint" });

  const probeInput = el("input", {
    type: "text",
    placeholder: "192.168.0.1",
    // Once a field has been touched it belongs to the user, and a refresh that arrives
    // mid-edit must not overwrite what is being typed.
    oninput: () => {
      probeTouched = true;
    },
  });

  const modeSelect = el("select", {
    onchange: () => {
      modeTouched = true;
      // The explanation attached to a mode is the point of having modes at all, so it follows
      // the selection immediately rather than waiting for the change to be applied.
      render(store.aps.value);
    },
  });
  for (const mode of PROBE_MODES) {
    modeSelect.append(el("option", { value: mode.value }, mode.label));
  }

  const hzInput = el("input", {
    type: "number",
    min: 0,
    max: PROBE_HZ_MAX,
    step: 10,
    placeholder: "100",
    oninput: () => {
      hzTouched = true;
    },
  });

  const applyButton = el(
    "button",
    {
      class: "button",
      title: "Send these probe settings to the host, without changing which radio is measured",
      onclick: () => {
        const state = store.aps.value;
        if (state === null) return;
        // Re-assert whatever is already being measured. A probe change is not a reason to move
        // the measurement, and in auto mode naming the current BSSID would quietly pin it.
        void choose(
          state.follow === "manual" && state.measured?.bssid
            ? { mode: "manual", bssid: state.measured.bssid, ...probeFields() }
            : { mode: "auto", ...probeFields() },
          "Applying the probe settings…",
        );
      },
    },
    "Apply probe settings",
  );

  let probeTouched = false;
  let modeTouched = false;
  let hzTouched = false;
  let busy = false;
  let refreshTimer: ReturnType<typeof setInterval> | null = null;

  function setOutcome(text: string, className = "") {
    outcome.textContent = text;
    outcome.className = `hint ${className}`;
  }

  /** Whichever probe mode the controls are currently showing, published or chosen. */
  function selectedMode(): ProbeMode | "" {
    const value = modeSelect.value;
    return PROBE_MODES.some((mode) => mode.value === value) ? (value as ProbeMode) : "";
  }

  /**
   * The probe settings to attach to a selection.
   *
   * An untouched control that the host has told us nothing about sends nothing: a missing field
   * means "leave it alone" on the server, and asserting a guess would report a setting to the
   * operator that they never chose and the host never had. Once the host publishes a value the
   * control is showing the truth, so sending it back is a no-op.
   */
  function probeFields(): ProbeFields {
    const fields: ProbeFields = { probe_host: probeInput.value.trim() };
    const mode = selectedMode();
    if (mode && (modeTouched || store.aps.value?.probe?.mode)) fields.probe_mode = mode;
    const hz = Number(hzInput.value);
    if (hzInput.value !== "" && Number.isFinite(hz) && (hzTouched || store.aps.value?.probe)) {
      fields.probe_hz = Math.max(0, Math.min(PROBE_HZ_MAX, hz));
    }
    return fields;
  }

  /**
   * Send a selection and report how it went.
   *
   * `doing` is passed in rather than derived from the request because the same request means
   * different things depending on which control raised it: re-asserting the BSSID that is
   * already being measured is how a probe change is applied, and reporting that as "moving the
   * measurement" would describe something the operator did not ask for.
   */
  async function choose(request: ApSelectRequest, doing?: string) {
    // One at a time. A second request while the host is restarting capture would race the first
    // and there is no sensible way to report the result of a race.
    if (busy) return;
    busy = true;
    setOutcome(
      doing ??
        (request.mode === "auto"
          ? "Handing the choice back to the dongle…"
          : `Moving the measurement to ${request.bssid}…`),
    );
    render(store.aps.value);

    const result = await store.selectAp(request);
    busy = false;
    if (result.ok) {
      setOutcome(
        "Done. Capture restarted — give the frame rate a few seconds to settle.",
        "hint-good",
      );
    } else {
      setOutcome(result.error || "The host could not make that change.", "hint-bad");
    }
    render(store.aps.value);
  }

  function summaryLine(label: string, value: string, className = ""): HTMLElement {
    return el(
      "div",
      { class: "ap-summary-line" },
      el("span", { class: "control-label" }, label),
      el("span", { class: `ap-meta ${className}` }, value),
    );
  }

  function describeUplink(state: ApStatus): string {
    if (state.uplink === "wired") return "wired — eth0 carries this node";
    if (state.uplink === "dongle") return "dongle — a USB radio carries this node";
    return "not reported";
  }

  function describeProbe(probe: ApProbe | undefined): string {
    // `!(hz > 0)` rather than `hz <= 0` so that a missing or unparsable rate reads as "off"
    // instead of as NaN Hz of probing.
    if (!probe || !(probe.hz > 0)) return "off — only what the network already carries";
    if (!probe.mode) return `${formatHz(probe.hz)} Hz, mode not reported`;
    return `${probe.mode} at ${formatHz(probe.hz)} Hz`;
  }

  /**
   * What the probing is actually buying, when the node has audited itself.
   *
   * This is the number that answers the only question that matters about a probe setting, and
   * it is why "induced" is shown before "total": a node can be receiving 200 Hz of somebody
   * else's evening video call and still be measuring nothing of its own making.
   */
  function renderYield(probe: ApProbe | undefined) {
    if (!probe || probe.induced_hz === undefined) {
      summary.append(summaryLine("Yield", "not measured"));
      return;
    }
    const parts = [`+${formatHz(probe.induced_hz)} Hz induced`];
    if (probe.total_hz !== undefined) parts.push(`${formatHz(probe.total_hz)} Hz total`);
    if (probe.background_hz !== undefined) {
      parts.push(`${formatHz(probe.background_hz)} Hz without probes`);
    }
    summary.append(summaryLine("Yield", parts.join(" · "), yieldClass(probe.induced_hz)));
  }

  function renderSummary(state: ApStatus) {
    const measured = state.measured?.bssid ?? "";
    const age = state.updated_at ? Math.max(0, Date.now() / 1000 - state.updated_at) : null;
    // Auto means "measure whatever the dongle associated to", so on a node whose uplink is the
    // wire there is nothing for it to follow and the button would be an invitation to break a
    // working capture.
    const wired = state.uplink === "wired";

    summary.append(
      summaryLine(
        "Measuring",
        measured
          ? `${measured}${state.measured?.chanspec ? ` · ${state.measured.chanspec}` : ""}`
          : "nothing yet",
      ),
      summaryLine(
        "Mode",
        state.follow === "manual"
          ? "manual — holding this BSSID"
          : "auto — following the dongle",
      ),
      summaryLine("Uplink", describeUplink(state)),
      summaryLine("Probe", describeProbe(state.probe)),
    );
    if (state.probe?.host) summary.append(summaryLine("Target", state.probe.host));
    renderYield(state.probe);
    summary.append(
      summaryLine("Scanned", age === null ? "—" : `${formatDuration(age)} ago`),
      el(
        "button",
        {
          class: `button ${state.follow === "auto" || wired ? "" : "button-primary"}`,
          disabled: busy || state.follow === "auto" || wired,
          title: wired
            ? "There is no associated radio to follow on a wired node — pick a BSSID instead"
            : "Measure whatever the dongle is associated to, and keep following it if it roams",
          onclick: () => void choose({ mode: "auto", ...probeFields() }),
        },
        wired
          ? "Nothing to follow (wired uplink)"
          : state.follow === "auto"
            ? "Following the dongle"
            : "Follow the dongle (auto)",
      ),
    );
  }

  /**
   * The physics behind the selected mode, because it decides whether the setting can work.
   *
   * Broadcast is the one worth spelling out: it reads as "make the access point shout", which is
   * exactly what it does not do and cannot do on this hardware.
   */
  function renderProbeNotes(state: ApStatus | null) {
    const mode = selectedMode();
    const wired = state?.uplink === "wired";

    if (mode === "broadcast") {
      probeNotes.append(
        el(
          "p",
          { class: "hint" },
          "Broadcast does not work by making the access point broadcast. Group-addressed " +
            "frames go out at a legacy rate and carry no HT channel estimate, so this hardware " +
            "produces no CSI for them at all. What it measures is the access point " +
            "acknowledging each probe we send it — so it only helps when the probe leaves by a " +
            "radio associated to that access point.",
        ),
        el(
          "p",
          { class: `hint ${wired ? "hint-bad" : ""}` },
          wired
            ? "This node's uplink is the wire, so it is associated to nothing and there are no " +
                "acknowledgements to measure. Use unicast at one of the measured access " +
                "point's own wireless clients instead."
            : "From a wired backbone there would be no association and nothing to acknowledge; " +
                "unicast at one of the access point's clients is the mode to use there.",
        ),
      );
    } else if (mode === "unicast") {
      probeNotes.append(
        el(
          "p",
          { class: "hint" },
          "UDP at the target below. This is the mode that works without an association: if the " +
            "target is a wireless client of the measured access point, that access point has to " +
            "put every packet on the air addressed to it, and that transmission is what gets " +
            "measured. The target never has to reply — it only has to be behind the right radio.",
        ),
      );
    } else if (mode === "icmp") {
      probeNotes.append(
        el(
          "p",
          { class: "hint" },
          "Ping the target and measure the replies. Much the weakest of the three — 200 Hz of " +
            "probes bought about 14 Hz here — because the replies come back through a remote " +
            "IP stack that rate-limits them and aggregates what is left. Worth it when you " +
            "also want proof the target is reachable.",
        ),
      );
    }

    // Only worth saying once there is a node to say it about. While the first fetch is in
    // flight, "no yield reported" would be describing our own ignorance, not the node's.
    if (state !== null && state.available !== false && state.probe?.induced_hz === undefined) {
      probeNotes.append(
        el(
          "p",
          { class: "hint" },
          "The node has not reported a measured yield, so nothing here shows whether these " +
            "probes are inducing traffic. Watch the frame rate after applying: if it does not " +
            "move, they are not.",
        ),
      );
    }
  }

  function renderRow(ap: AccessPoint, state: ApStatus) {
    const isMeasured = sameMac(ap.bssid, state.measured?.bssid);
    // Measured is not the same as chosen. In auto mode this radio is only being measured
    // because the dongle happens to be associated to it, and it stops being measured the moment
    // the dongle roams — so it is still worth offering to pin, and only a pinned radio has
    // nothing left to ask for.
    const isPinned = isMeasured && state.follow === "manual";
    // The percentage nmcli reports is a quality figure, not power, so the bar is only ever a
    // rough "which of these is loud" — which is exactly what it is being used for here.
    const strength = Math.max(0, Math.min(100, ap.signal));
    // With no managed radio the list can only come from the monitor interface, which never sees
    // an SSID (see the note under the list). A nameless entry there is expected rather than a
    // hidden network, and saying so on every row would drown out the BSSIDs.
    const name = ap.ssid || (state.uplink === "wired" ? "(no name)" : "(hidden network)");

    list.append(
      el(
        "div",
        { class: `ap ${isMeasured ? "ap-measured" : ""}` },
        el(
          "div",
          { class: "ap-main" },
          el(
            "div",
            { class: "ap-name" },
            name,
            isMeasured ? el("span", { class: "ap-badge ap-badge-measured" }, "measured") : null,
            ap.in_use ? el("span", { class: "ap-badge ap-badge-dongle" }, "dongle") : null,
          ),
          el("div", { class: "ap-meta" }, `${ap.bssid} · ch ${ap.channel} · ${ap.signal}%`),
        ),
        el("div", { class: "ap-signal" }, el("i", { style: `width:${strength}%` })),
        el(
          "button",
          {
            class: "button",
            disabled: busy || isPinned,
            title: isMeasured
              ? "Hold this radio even if the dongle associates somewhere else"
              : "Point the monitor radio at this one. The uplink is untouched.",
            onclick: () =>
              void choose({ mode: "manual", bssid: ap.bssid, ...probeFields() }),
          },
          isPinned ? "Measuring" : isMeasured ? "Pin" : "Measure",
        ),
      ),
    );
  }

  function render(state: ApStatus | null) {
    clear(list);
    clear(summary);
    clear(probeNotes);
    applyButton.disabled = busy || state === null || state.available === false;

    if (state === null) {
      list.append(el("p", { class: "hint" }, "Asking the host which radios it can hear…"));
      renderProbeNotes(state);
      return;
    }

    if (state.available === false) {
      list.append(
        el(
          "p",
          { class: "hint" },
          "The host is not publishing a radio list. The agent that scans and applies these " +
            "choices runs outside the container — check csi-ap-agent on the Pi, and that the " +
            "data directory really is shared with it.",
        ),
      );
      renderProbeNotes(state);
      return;
    }

    if (!probeTouched && state.probe?.host) probeInput.value = state.probe.host;
    if (!modeTouched && state.probe?.mode) modeSelect.value = state.probe.mode;
    if (!hzTouched && Number.isFinite(state.probe?.hz)) hzInput.value = String(state.probe?.hz);
    // Which interface would be lost with a bad choice depends on which one carries the node, and
    // that is exactly the thing this page has to be trustworthy about.
    routeNote.textContent =
      state.uplink === "wired"
        ? "This node is reached over eth0 and captures on a radio that is associated to " +
          "nothing, so no choice here touches the route in at all."
        : "Changing what is measured never touches the dongle's association, which is the only " +
          "route into the Pi. A choice here can waste a measurement; it cannot lose the machine.";

    renderSummary(state);
    renderProbeNotes(state);

    if (state.aps.length === 0) {
      list.append(el("p", { class: "hint" }, "The last scan found nothing in range."));
      return;
    }

    // Loudest first. It is the order the list is easiest to read in, even though the loudest is
    // often the one you least want.
    const sorted = [...state.aps].sort(
      (a, b) => b.signal - a.signal || a.bssid.localeCompare(b.bssid),
    );
    for (const ap of sorted) renderRow(ap, state);

    if (state.uplink === "wired" && sorted.some((ap) => !ap.ssid)) {
      list.append(
        el(
          "p",
          { class: "hint" },
          "No names, because there is no managed radio to scan with: this list comes from the " +
            "monitor interface itself. An SSID travels in beacons, beacons are legacy frames, " +
            "and this extractor produces nothing for those — so a BSSID is all there is to go " +
            "on here.",
        ),
      );
    }
  }

  const root = viewLayout({
    main: [
      el(
        "div",
        { class: "panel panel-grow" },
        el("div", { class: "panel-head" }, el("h2", {}, "Radios in range")),
        list,
        el(
          "p",
          { class: "hint" },
          "On a mesh the nearest radio is usually the worst sensor: nothing moves between it " +
            "and the Pi, so the channel it gives you is clean and dead. Pick the one whose " +
            "path crosses the room you are trying to measure.",
        ),
      ),
    ],
    side: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Current")),
        summary,
        outcome,
      ),
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Provoking traffic")),
        el(
          "label",
          { class: "control" },
          el("span", { class: "control-label" }, "Mode"),
          modeSelect,
        ),
        el(
          "label",
          { class: "control" },
          el("span", { class: "control-label" }, "Rate (Hz)"),
          hzInput,
        ),
        el(
          "label",
          { class: "control" },
          el("span", { class: "control-label" }, "Target"),
          probeInput,
        ),
        applyButton,
        probeNotes,
        el(
          "p",
          { class: "hint" },
          "A rate of 0 measures only what the network already carries, which overnight is close " +
            "to nothing. Applying restarts the capture.",
        ),
        routeNote,
      ),
    ],
  });

  const unsubscribes: (() => void)[] = [];

  return {
    id: "aps",
    title: "Access point",
    root,
    mount() {
      unsubscribes.push(store.aps.subscribe(render));
      void store.refreshAps();
      refreshTimer = setInterval(() => void store.refreshAps(), REFRESH_MS);
    },
    unmount() {
      while (unsubscribes.length > 0) unsubscribes.pop()?.();
      if (refreshTimer !== null) clearInterval(refreshTimer);
      refreshTimer = null;
    },
  };
}
