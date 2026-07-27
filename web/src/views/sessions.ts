// Browse recordings, scrub, replay into the live views.
//
// This is the view that makes the rest of the project tractable: with it, iterating on the
// breathing detector is a matter of replaying `seated-still` at 10x, not of going and lying on
// the floor again. The capture labels below are the ones from the plan's table, offered as
// buttons so a session gets labelled while you still remember what it was.

import { clear, el, formatBytes, formatDuration, viewLayout } from "../lib/dom";
import type { ReplayState, Session } from "../lib/messages";
import { store } from "../lib/store";
import type { View } from "./view";

const SUGGESTED: { label: string; duration: string; purpose: string }[] = [
  { label: "empty-room", duration: "30 min", purpose: "Baseline / noise floor" },
  { label: "walking", duration: "5 min", purpose: "Obvious motion, sanity check" },
  { label: "seated-still", duration: "10 min", purpose: "Breathing target" },
  { label: "seated-still-positions", duration: "5 min × 6", purpose: "Fresnel-zone mapping" },
  { label: "overnight", duration: "8 h", purpose: "Long-run stability, drift" },
];

async function post(path: string, body?: unknown): Promise<Response> {
  return fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export function sessionsView(): View {
  const list = el("div", { class: "session-list" });
  const recordingBox = el("div", { class: "controls" });
  const replayBox = el("div", { class: "controls" });

  function renderRecording(active: Session | null) {
    clear(recordingBox);

    if (active !== null) {
      recordingBox.append(
        el(
          "div",
          { class: "recording-active" },
          el("span", { class: "dot dot-recording" }),
          el("strong", {}, active.label),
          ` · ${active.frames.toLocaleString()} frames · ${formatDuration(active.duration_s)}`,
        ),
        el(
          "button",
          {
            class: "button button-danger",
            onclick: async () => {
              await post("/api/recording/stop");
              void store.refreshSessions();
            },
          },
          "Stop recording",
        ),
      );
      return;
    }

    const input = el("input", { type: "text", placeholder: "label", value: "" });
    const notes = el("input", { type: "text", placeholder: "notes (optional)" });

    const start = async (label: string) => {
      await post("/api/sessions", { label, notes: notes.value });
      void store.refreshSessions();
    };

    recordingBox.append(
      el("label", { class: "control" }, el("span", { class: "control-label" }, "Label"), input),
      el("label", { class: "control" }, el("span", { class: "control-label" }, "Notes"), notes),
      el(
        "button",
        {
          class: "button button-primary",
          onclick: () => void start(input.value.trim() || "unlabelled"),
        },
        "Start recording",
      ),
      el("div", { class: "control-label" }, "Or one of the planned captures:"),
      el(
        "div",
        { class: "chip-row" },
        ...SUGGESTED.map((suggestion) =>
          el(
            "button",
            {
              class: "chip",
              title: `${suggestion.duration} — ${suggestion.purpose}`,
              onclick: () => void start(suggestion.label),
            },
            suggestion.label,
          ),
        ),
      ),
    );
  }

  function renderReplay(state: ReplayState | null) {
    clear(replayBox);

    if (state === null) {
      replayBox.append(
        el(
          "p",
          { class: "hint" },
          "Nothing replaying. Replayed frames go through the exact same pipeline as live ones, " +
            "so every view below works on a recording — and live frames are ignored while a " +
            "replay runs, so the two rooms never mix.",
        ),
      );
      return;
    }

    const span =
      state.first_t_us !== null && state.last_t_us !== null
        ? state.last_t_us - state.first_t_us
        : 0;
    const position = span > 0 ? (state.position_us - (state.first_t_us ?? 0)) / span : 0;

    const scrub = el("input", {
      type: "range",
      min: 0,
      max: 1000,
      step: 1,
      value: Math.round(position * 1000),
      onchange: (event: Event) => {
        const fraction = Number((event.target as HTMLInputElement).value) / 1000;
        const target = (state.first_t_us ?? 0) + fraction * span;
        void post("/api/replay/control", { action: "seek", t_us: Math.round(target) });
      },
    });

    replayBox.append(
      el(
        "div",
        { class: "replay-head" },
        el("span", { class: "dot dot-replay" }),
        el("strong", {}, state.path),
        state.finished ? " · finished" : state.playing ? " · playing" : " · paused",
        ` · ${formatDuration(state.position_us / 1e6)} of ${formatDuration(span / 1e6)}`,
      ),
      scrub,
      el(
        "div",
        { class: "button-row" },
        el(
          "button",
          {
            class: "button",
            onclick: () =>
              void post("/api/replay/control", { action: state.playing ? "pause" : "resume" }),
          },
          state.playing ? "Pause" : "Play",
        ),
        ...[0.5, 1, 2, 10, 0].map((speed) =>
          el(
            "button",
            {
              class: `chip ${state.speed === speed ? "chip-active" : ""}`,
              title: speed === 0 ? "As fast as the machine can go" : `${speed}x`,
              onclick: () => void post("/api/replay/control", { action: "speed", speed }),
            },
            speed === 0 ? "max" : `${speed}×`,
          ),
        ),
        el(
          "button",
          {
            class: "button button-danger",
            onclick: () => void post("/api/replay/stop"),
          },
          "Stop replay",
        ),
      ),
    );
  }

  function renderSessions(sessions: Session[]) {
    clear(list);

    if (sessions.length === 0) {
      list.append(el("p", { class: "hint" }, "No recordings yet."));
      return;
    }

    for (const session of sessions) {
      const label = el("input", {
        class: "session-label",
        type: "text",
        value: session.label,
        onchange: (event: Event) => {
          void fetch(`/api/sessions/${session.id}`, {
            method: "PATCH",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ label: (event.target as HTMLInputElement).value }),
          }).then(() => store.refreshSessions());
        },
      });

      list.append(
        el(
          "div",
          { class: `session ${session.active ? "session-active" : ""}` },
          el(
            "div",
            { class: "session-main" },
            label,
            el(
              "div",
              { class: "session-meta" },
              `${formatDuration(session.duration_s)} · ${session.frames.toLocaleString()} frames · ` +
                `${formatBytes(session.bytes)} · nodes ${session.node_ids.join(", ") || "—"} · ` +
                `${session.n_sub || "?"} subcarriers`,
              session.notes ? el("div", { class: "session-notes" }, session.notes) : null,
            ),
          ),
          el(
            "div",
            { class: "button-row" },
            el(
              "button",
              {
                class: "button",
                disabled: session.active,
                onclick: () => void post(`/api/sessions/${session.id}/replay`, { speed: 1 }),
              },
              "Replay",
            ),
            el(
              "button",
              {
                class: "button",
                disabled: session.active,
                title: "Replay as fast as the machine allows — an overnight run in a minute",
                onclick: () => void post(`/api/sessions/${session.id}/replay`, { speed: 0 }),
              },
              "Fast",
            ),
            el(
              "button",
              {
                class: "button",
                title: "Rebuild frame counts by walking the file — use after an unclean shutdown",
                onclick: () =>
                  void post(`/api/sessions/${session.id}/rescan`).then(() =>
                    store.refreshSessions(),
                  ),
              },
              "Rescan",
            ),
            el(
              "button",
              {
                class: "button button-danger",
                disabled: session.active,
                onclick: () => {
                  if (!confirm(`Delete ${session.label}? The recording file goes too.`)) return;
                  void fetch(`/api/sessions/${session.id}`, { method: "DELETE" }).then(() =>
                    store.refreshSessions(),
                  );
                },
              },
              "Delete",
            ),
          ),
        ),
      );
    }
  }

  const root = viewLayout({
    main: [
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Recording")),
        recordingBox,
      ),
      el(
        "div",
        { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", {}, "Replay")),
        replayBox,
      ),
      el(
        "div",
        { class: "panel panel-grow" },
        el("div", { class: "panel-head" }, el("h2", {}, "Sessions")),
        list,
      ),
    ],
  });

  const unsubscribes: (() => void)[] = [];

  return {
    id: "sessions",
    title: "Sessions",
    root,
    mount() {
      unsubscribes.push(store.recording.subscribe(renderRecording));
      unsubscribes.push(store.replay.subscribe(renderReplay));
      unsubscribes.push(store.sessions.subscribe(renderSessions));
      void store.refreshSessions();
    },
    unmount() {
      while (unsubscribes.length > 0) unsubscribes.pop()?.();
    },
  };
}
