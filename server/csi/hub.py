"""The centre of the server: one datagram in, everything else out.

Every frame — live from UDP or replayed from disk — enters through `handle_datagram` and takes
exactly the same route: parse, health accounting, record, preprocess, push to the node's ring,
fan out to clients. Analysis runs on a separate slow timer over the rings, because presence and
breathing want a window, not a frame, and there is no reason to run an SVD at 80 Hz.

That timer hands the actual DSP to a worker thread. An SVD and two filtfilts on the event loop
are milliseconds that the UDP listener and the WebSocket writers spend waiting, and the socket
buffer is the only thing absorbing it; the cost shows up as jitter in the very timestamps the
analysis then measures its sample rate from. NumPy and SciPy drop the GIL for most of that work,
so moving it off the loop is close to free — provided the thread never touches the live ring.
`history_for` is that boundary, and `NodeState` defers detector mutations across it.

Keeping live and replay on one path is not a tidiness preference. It is the thing that makes a
recording a valid substitute for the room, which is what lets the detector be developed at a
desk instead of on the floor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import Settings
from .downlink import encode_frame
from .dsp.preprocess import Preprocessor, Processed
from .dsp.presence import PresenceDetector
from .dsp.selection import (
    BREATHING_BAND,
    HEART_BAND,
    SelectionConfig,
    band_snr_db,
    rank_for_band,
)
from .dsp.subcarriers import describe
from .dsp.vitals import VitalsEstimator
from .nodes import NodeHealth
from .protocol import ProtocolError, parse_frame
from .recorder import Recorder
from .replay import Replayer
from .ring import FrameRing, History, Window
from .sessions import SessionStore

log = logging.getLogger("csi.hub")

# A client that cannot keep up gets its oldest frames dropped rather than blocking ingest. A
# second of history is plenty of slack for a browser tab that briefly went to the background.
CLIENT_QUEUE = 256


def _same_mask(a: np.ndarray, b: np.ndarray) -> bool:
    """`valid_mask` is memoized and returns a read-only shared array, so the identity check hits
    on every frame that did not change the mask — which is all of them but one."""
    return a is b or np.array_equal(a, b)


def _section(patch: dict, name: str) -> dict:
    """One block of a config patch. A non-object where an object was expected is an empty one —
    `patch["presence"] = 3` should change nothing, not raise."""
    section = patch.get(name)
    return section if isinstance(section, dict) else {}


def _number(value: Any, minimum: float, maximum: float) -> float | None:
    """A client-supplied number, or None if it is not one.

    `bool` is rejected before anything else because `True` is an `int` in Python: a checkbox
    posted into a numeric field would otherwise set a one-second analysis window and look, from
    the config echo, like a deliberate choice.

    Out of range is rejected rather than clamped. Clamping answers a nonsensical request with a
    plausible-looking number, and the config echo the UI renders from would then disagree with
    what was asked for while giving no sign that anything was refused.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or not (minimum <= out <= maximum):
        return None
    return out


@dataclass(eq=False)  # identity, not value: clients live in a set and two are never the same
class Client:
    """One WebSocket subscriber."""

    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE))
    nodes: set[int] | None = None  # None means all
    frames: bool = True  # binary CSI frames; a client watching only metrics can turn this off
    decimate: int = 1
    _counter: int = 0
    dropped: int = 0

    def wants(self, node_id: int) -> bool:
        if not self.frames:
            return False
        if self.nodes is not None and node_id not in self.nodes:
            return False
        if self.decimate > 1:
            self._counter += 1
            if self._counter % self.decimate:
                return False
        return True

    def send(self, message: Any) -> None:
        """Never blocks and never raises. A slow client degrades itself, not the pipeline."""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(message)


class NodeState:
    """Everything the server knows about one node."""

    def __init__(self, node_id: int, settings: Settings) -> None:
        self.node_id = node_id
        self.settings = settings
        self.health = NodeHealth(node_id=node_id)
        self.pre = Preprocessor(settings.preprocess)
        self.presence = PresenceDetector(settings.presence)
        self.breathing = VitalsEstimator(settings.breathing)
        self.heart = VitalsEstimator(settings.heart)
        self.ring: FrameRing | None = None
        self.last_metrics: dict = {}

        # Set while this node's analysis is running in a worker thread. The presence detector
        # is stateful — a baseline, a calibration accumulator, a debounced state machine — so
        # anything on the event loop that would rewind it waits rather than racing it.
        self.analyzing = False
        self._pending: str | None = None

    def ensure_ring(self, processed: Processed) -> FrameRing:
        """The ring for this node, rebuilt or cleared when the frames stop being comparable.

        Two things make a stored row incomparable with a new one, and only the first is obvious.
        The bandwidth changing moves `n_sub`, so the matrix no longer fits and the ring is
        rebuilt.

        A mask change does not move anything. The array is the same width, and the rows written
        under the old mask carry NaN at exactly the subcarriers the new mask calls valid — so a
        window straddling the change is a matrix with NaN columns that the analyzers select
        *because* the mask says they are good. Nothing downstream tolerates that:
        `uniform_resample` smears the NaN across the column, `welch` refuses outright, and the
        whole metrics pass dies. It stays dead until the last pre-change row ages out of the
        ring, which at the default history is two minutes of no breathing, no presence and no
        node health for every node — from unticking one checkbox in the UI.

        So a mask change is treated as the discontinuity it is: throw the history away, exactly
        as for a reboot. Comparing the mask itself rather than the setting that produced it
        means any future reason for the mask to move is covered by the same check.
        """
        n_sub = processed.frame.n_sub
        if self.ring is None or self.ring.n_sub != n_sub:
            self.ring = FrameRing(n_sub, self.settings.ring_capacity())
        elif len(self.ring) > 0 and not _same_mask(self.ring.mask, processed.mask):
            log.info("node %d: subcarrier mask changed; clearing history", self.node_id)
            self.reset()
        return self.ring

    def reset(self) -> None:
        """Forget history. Used on node reboot and when a replay starts or seeks — carrying a
        window across a discontinuity puts a step change into every analysis at once."""
        self._request("reset")

    def recalibrate(self) -> None:
        """Throw away the presence baseline but keep the history."""
        self._request("recalibrate")

    def _request(self, op: str) -> None:
        if self.analyzing:
            # A half-applied recalibration is the failure this avoids: the worker appending to
            # a calibration accumulator that the loop just cleared leaves a baseline measured
            # over both sides of the discontinuity, which is how you get a detector that never
            # fires again. `reset` subsumes `recalibrate`, so it wins if both are asked for.
            self._pending = "reset" if "reset" in (op, self._pending) else op
            return
        self._apply(op)

    def _apply(self, op: str) -> None:
        self.presence.recalibrate()
        if op == "reset":
            self.pre.reset()
            if self.ring is not None:
                self.ring.clear()

    def take_pending(self) -> bool:
        """Apply whatever was deferred during analysis. True if the in-flight result is stale."""
        op, self._pending = self._pending, None
        if op is None:
            return False
        self._apply(op)
        return True


class Hub:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        self.sessions = SessionStore(self.settings.recordings_dir)

        self.nodes: dict[int, NodeState] = {}
        self.clients: set[Client] = set()

        self.recorder: Recorder | None = None
        self.replayer: Replayer | None = None
        self._replay_task: asyncio.Task | None = None

        self.started_at = time.time()
        self.bad_packets = 0
        self.live_frames = 0
        self.replay_frames = 0
        self.suppressed_live = 0

        self._metrics_task: asyncio.Task | None = None
        self._stopping = False

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> None:
        self._metrics_task = asyncio.create_task(self._metrics_loop(), name="csi-metrics")
        if self.settings.record:
            self.start_recording("live")

    async def stop(self) -> None:
        # Closing the recorder is the one step here that has data riding on it: it is what writes
        # `ended_at` and flushes the final session metadata. It goes in a `finally` so no earlier
        # step can skip it — an unclean shutdown costs a `rescan`, and this is the shutdown path
        # that is supposed to be clean.
        self._stopping = True
        try:
            await self.stop_replay()
            if self._metrics_task:
                self._metrics_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._metrics_task
        finally:
            self.stop_recording()

    # -- the one path in ------------------------------------------------------------------

    def handle_datagram(self, datagram: bytes, received_at: float, *, replay: bool = False) -> None:
        """Entry point for both the UDP listener and the replayer."""
        if not replay and self.replayer is not None and not self.replayer.finished:
            # A replay owns the node IDs while it runs. Mixing recorded and live frames from
            # the same node into one ring would interleave two different rooms.
            self.suppressed_live += 1
            return

        try:
            frame = parse_frame(datagram, received_at=received_at)
        except ProtocolError as exc:
            self.bad_packets += 1
            if self.bad_packets < 10 or self.bad_packets % 1000 == 0:
                log.warning("dropping malformed datagram (%d so far): %s", self.bad_packets, exc)
            return

        state = self.nodes.get(frame.node_id)
        if state is None:
            state = NodeState(frame.node_id, self.settings)
            self.nodes[frame.node_id] = state
            log.info("node %d appeared: %d subcarriers", frame.node_id, frame.n_sub)

        roams_before = state.health.roams
        if state.health.observe(frame, now=received_at):
            if state.health.roams > roams_before:
                # Not an error, and on a mesh network not even unusual — but it does cost the
                # presence detector its calibration, so it is logged at the same level as a
                # reboot rather than buried. A node that does this repeatedly wants CSI_LOCK_BSSID.
                log.info(
                    "node %d re-associated with %s (link epoch %d); clearing history",
                    frame.node_id,
                    state.health.src_mac.hex(":"),
                    frame.link_epoch,
                )
            else:
                log.info("node %d rebooted; clearing history", frame.node_id)
            state.reset()

        if replay:
            self.replay_frames += 1
        else:
            self.live_frames += 1
            if self.recorder is not None:
                self.recorder.write(datagram, frame)

        processed = state.pre.process(frame)
        state.ensure_ring(processed).push(processed)
        self._fan_out_frame(processed, replay=replay)

    def _fan_out_frame(self, processed: Processed, *, replay: bool) -> None:
        if not self.clients:
            return
        node_id = processed.frame.node_id
        payload: bytes | None = None
        for client in self.clients:
            if not client.wants(node_id):
                continue
            if payload is None:
                payload = encode_frame(processed, replay=replay)
            client.send(payload)

    def broadcast(self, event: dict) -> None:
        """Send a JSON event to every client."""
        for client in self.clients:
            client.send(event)

    # -- clients --------------------------------------------------------------------------

    def add_client(self, client: Client) -> None:
        self.clients.add(client)
        client.send({"type": "hello", **self.snapshot()})

    def remove_client(self, client: Client) -> None:
        self.clients.discard(client)

    # -- recording ------------------------------------------------------------------------

    def start_recording(self, label: str, notes: str = "") -> dict:
        self.stop_recording()
        session = self.sessions.create(label, notes)
        self.recorder = Recorder(self.sessions, session)
        log.info("recording session %s (%s)", session.id, label)
        self.broadcast({"type": "recording", "session": session.as_dict()})
        return session.as_dict()

    def stop_recording(self) -> dict | None:
        if self.recorder is None:
            return None
        session = self.recorder.session
        self.recorder.close()
        self.recorder = None
        log.info("closed session %s: %d frames", session.id, session.frames)
        self.broadcast({"type": "recording", "session": None})
        return session.as_dict()

    # -- replay ---------------------------------------------------------------------------

    async def start_replay(
        self,
        session_id: str,
        *,
        speed: float = 1.0,
        loop: bool = False,
        start_us: int | None = None,
    ) -> dict:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        path = self.sessions.file_for(session)
        if not path.exists():
            raise FileNotFoundError(str(path))

        await self.stop_replay()
        for state in self.nodes.values():
            state.reset()

        self.replayer = Replayer(
            path,
            self._replay_sink,
            speed=speed,
            loop=loop,
            on_state=lambda st: self.broadcast(
                {"type": "replay", "replay": st, "session_id": session_id}
            ),
        )
        if start_us is not None:
            self.replayer.seek(start_us)
        self._replay_task = asyncio.create_task(self.replayer.run(), name="csi-replay")
        state = self.replayer.state()
        self.broadcast({"type": "replay", "replay": state, "session_id": session_id})
        return state

    async def stop_replay(self) -> None:
        """Tear the replay down. Never raises: every caller is a cleanup path.

        The two-second wait is a backstop, not the mechanism — `Replayer.stop()` interrupts the
        pump directly. If it were ever to expire, `wait_for` cancels the task and raises
        TimeoutError, and letting that out is worse than the stall it reports: `stop_replay` is
        what `Hub.stop()` calls first, so an exception here skips closing the recorder, and the
        session is left with no `ended_at` — marked active forever, which the UI reads as "still
        recording" and refuses to replay or delete.
        """
        if self.replayer is None:
            return
        self.replayer.stop()
        try:
            if self._replay_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(self._replay_task, timeout=2.0)
        except TimeoutError:
            log.warning("replay task did not stop within 2 s; cancelled it and moved on")
        finally:
            self.replayer = None
            self._replay_task = None
            for state in self.nodes.values():
                state.reset()
            self.broadcast({"type": "replay", "replay": None})

    def _replay_sink(self, datagram: bytes, received_at: float) -> None:
        self.handle_datagram(datagram, received_at, replay=True)

    def seek_replay(self, t_us: int) -> None:
        if self.replayer is not None:
            self.replayer.seek(t_us)
            for state in self.nodes.values():
                state.reset()

    # -- analysis -------------------------------------------------------------------------

    async def _metrics_loop(self) -> None:
        period = 1.0 / max(self.settings.metrics_hz, 0.1)
        while not self._stopping:
            await asyncio.sleep(period)
            try:
                await self._emit_metrics()
            except Exception:  # a bad window must not kill the loop for the rest of the session
                log.exception("metrics computation failed")

    async def _emit_metrics(self) -> None:
        """Compute always; broadcast only when someone is listening.

        Skipping the computation when no browser is connected looks like an easy saving and is
        a bug. The presence detector is stateful: it needs 30 s of quiet to calibrate, and it
        drifts its baseline while the room reads as empty. If it only runs while a tab is open,
        then opening a tab starts a calibration over whatever happens to be in the ring —
        which, if you walked over to the laptop to open it, is you walking. The observed
        symptom is an empty-room threshold hundreds of times too high and a detector that never
        fires again.

        It is also just wrong for the product: an overnight activity record is not supposed to
        depend on a browser being open all night.
        """
        now = time.time()
        # Iterate a copy: awaiting below lets ingest run, and a node appearing mid-pass would
        # otherwise resize the dict underneath us.
        for state in list(self.nodes.values()):
            history = self.history_for(state)
            if history is None:
                continue
            state.analyzing = True
            try:
                metrics = await asyncio.to_thread(self.compute_metrics, state, history)
            finally:
                state.analyzing = False
            if state.take_pending():
                # A reboot, a replay seek or a recalibrate landed while that ran. The result
                # describes a room that no longer exists as far as this node is concerned.
                continue
            if metrics is None:
                continue
            state.last_metrics = metrics
            if self.clients:
                self.broadcast({"type": "metrics", "t": now, **metrics})
        if self.clients:
            self.broadcast({"type": "nodes", "nodes": self.node_report(now)})

    def history_for(self, state: NodeState) -> Window | None:
        """Copy a node's history out of the live ring, on the event loop.

        This is what makes threading the DSP safe. The analyzers reach for windows as they go;
        a worker doing that against the live ring would be reading a buffer the ingest task is
        writing, and would eventually get a window torn across the write pointer — samples from
        two different points in time in one array, spliced at whatever index the writer had
        reached. One copy of the longest span anyone will ask for costs a few hundred kilobytes
        and a memcpy, and every sub-window then comes off that frozen copy.

        Note this is a *narrower* copy than the ring: analysis never looks past the longest
        configured window, so there is no reason to snapshot the full two minutes of history.
        """
        ring = state.ring
        if ring is None or len(ring) < 16:
            return None
        s = self.settings
        span_s = max(s.presence.window_s, s.breathing.window_s, s.heart.window_s)
        history = ring.seconds(span_s)
        return history if len(history) >= 16 else None

    def compute_metrics(self, state: NodeState, history: History) -> dict | None:
        """Runs in a worker thread over a snapshot from `history_for`, never `state.ring`."""
        if len(history) < 16:
            return None

        out: dict = {"node_id": state.node_id, "n_sub": history.n_sub}

        presence = state.presence.update(history)
        if presence is not None:
            out["presence"] = presence.as_dict()

        breathing = state.breathing.estimate(history)
        if breathing is not None:
            out["breathing"] = breathing.as_dict()
        elif state.breathing.last_rejection:
            # Named rather than silent. A breathing panel that just goes blank looks like a
            # broken server; one that says the link had a two-second hole in it points at the
            # actual problem, which on a shared access point is usually the network being busy.
            out["breathing_rejected"] = state.breathing.last_rejection

        if self.settings.heart.window_s > 0 and len(history) > 64:
            heart = state.heart.estimate(history)
            if heart is not None:
                out["heart"] = heart.as_dict()
            elif state.heart.last_rejection:
                out["heart_rejected"] = state.heart.last_rejection

        # The placement tuner wants one number that responds immediately to the node being
        # moved, so it runs on a short window regardless of the breathing window setting.
        tuner_window = history.seconds(min(20.0, self.settings.breathing.window_s))
        if len(tuner_window) > 32:
            out["placement"] = {
                "breathing_snr_db": round(band_snr_db(tuner_window, BREATHING_BAND), 2),
                "heart_snr_db": round(band_snr_db(tuner_window, HEART_BAND), 2),
            }
            ranked = rank_for_band(
                tuner_window,
                SelectionConfig(
                    gate_quantile=self.settings.breathing.selection.gate_quantile,
                    top_k=self.settings.breathing.selection.top_k,
                ),
                band=BREATHING_BAND,
            )
            out["selection"] = ranked.as_dict()

        window = history.seconds(self.settings.presence.window_s)
        if len(window) > 4:
            valid = np.flatnonzero(window.mask)
            out["variance"] = {
                "subcarriers": valid.tolist(),
                "values": [
                    round(float(v), 5) for v in np.nanvar(window.amp[:, valid], axis=0)
                ],
                "agc_fraction": round(float(window.agc.mean()), 4),
            }
            out["rate_hz"] = round(window.rate_hz, 2)

        return out

    # -- introspection --------------------------------------------------------------------

    def node_report(self, now: float | None = None) -> list[dict]:
        now = now if now is not None else time.time()
        return [
            state.health.as_dict(self.settings.node_timeout_s, now=now)
            for state in sorted(self.nodes.values(), key=lambda s: s.node_id)
        ]

    def snapshot(self) -> dict:
        n_sub = next((s.ring.n_sub for s in self.nodes.values() if s.ring), 0)
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "nodes": self.node_report(),
            "layout": describe(n_sub) if n_sub else None,
            "recording": self.recorder.session.as_dict() if self.recorder else None,
            "replay": self.replayer.state() if self.replayer else None,
            "counters": {
                "live_frames": self.live_frames,
                "replay_frames": self.replay_frames,
                "bad_packets": self.bad_packets,
                "suppressed_live": self.suppressed_live,
                "clients": len(self.clients),
            },
            "config": self.config_dict(),
        }

    def config_dict(self) -> dict:
        s = self.settings
        return {
            "preprocess": {
                "norm_mode": s.preprocess.norm_mode,
                "drop_pilots": s.preprocess.drop_pilots,
                "hampel_enabled": s.preprocess.hampel_enabled,
                "agc_step_db": s.preprocess.agc_step_db,
                "agc_uniformity": s.preprocess.agc_uniformity,
            },
            "presence": {
                "window_s": s.presence.window_s,
                "calibration_s": s.presence.calibration_s,
                "enter_sigma": s.presence.enter_sigma,
                "exit_sigma": s.presence.exit_sigma,
                "debounce_s": s.presence.debounce_s,
                "use_pca": s.presence.use_pca,
                "gate_quantile": s.presence.selection.gate_quantile,
                "top_k": s.presence.selection.top_k,
            },
            "breathing": {
                "window_s": s.breathing.window_s,
                "band": list(s.breathing.band),
                "n_subcarriers": s.breathing.n_subcarriers,
                "gate_quantile": s.breathing.selection.gate_quantile,
            },
            "heart": {
                "window_s": s.heart.window_s,
                "band": list(s.heart.band),
                "n_subcarriers": s.heart.n_subcarriers,
                "gate_quantile": s.heart.selection.gate_quantile,
            },
        }

    def update_config(self, patch: dict) -> dict:
        """Apply a partial config update from the UI.

        Every value is coerced through `_number`, which returns None for anything that is not a
        finite number in range — and a key whose value does not survive that is dropped, exactly
        as an unknown key is. The rule the whole method is written to keep is that a client
        cannot crash the server with a typo, and it was only half kept: unknown *keys* were
        ignored, but a known key holding `"20"` or `null` reached a bare `float()` and raised.

        That is not a hypothetical. This runs on two paths and the raise is worse on both. Over
        HTTP it is a 500. Over the WebSocket it unwinds through `_handle_client_message` into the
        receive loop, which catches only `WebSocketDisconnect` and `RuntimeError`, so the socket
        is torn down and the browser reconnects with every view's state lost — from one slider.
        """
        s = self.settings

        pre = _section(patch, "preprocess")
        if pre.get("norm_mode") in ("hybrid", "rssi", "rms", "none"):
            s.preprocess.norm_mode = pre["norm_mode"]
        for key in ("drop_pilots", "hampel_enabled"):
            if key in pre:
                setattr(s.preprocess, key, bool(pre[key]))
        for key, lo, hi in (("agc_step_db", 0.0, 60.0), ("agc_uniformity", 0.0, 10.0)):
            value = _number(pre.get(key), lo, hi) if key in pre else None
            if value is not None:
                setattr(s.preprocess, key, value)

        pres = _section(patch, "presence")
        for key, lo, hi in (
            ("window_s", 0.1, 300.0),
            ("calibration_s", 1.0, 3600.0),
            ("enter_sigma", 0.0, 1000.0),
            ("exit_sigma", 0.0, 1000.0),
            ("debounce_s", 0.0, 600.0),
        ):
            value = _number(pres.get(key), lo, hi) if key in pres else None
            if value is not None:
                setattr(s.presence, key, value)
        if "use_pca" in pres:
            s.presence.use_pca = bool(pres["use_pca"])
        gate = _number(pres.get("gate_quantile"), 0.0, 0.95)
        if gate is not None:
            s.presence.selection.gate_quantile = gate
        top_k = _number(pres.get("top_k"), 1, 512)
        if top_k is not None:
            s.presence.selection.top_k = int(top_k)

        for name, cfg in (("breathing", s.breathing), ("heart", s.heart)):
            patch_cfg = _section(patch, name)
            window_s = _number(patch_cfg.get("window_s"), 0.1, 300.0)
            if window_s is not None:
                cfg.window_s = window_s
            n_subcarriers = _number(patch_cfg.get("n_subcarriers"), 1, 512)
            if n_subcarriers is not None:
                cfg.n_subcarriers = int(n_subcarriers)
            band = patch_cfg.get("band")
            if isinstance(band, (list, tuple)) and len(band) == 2:
                lo = _number(band[0], 0.0, 1e6)
                hi = _number(band[1], 0.0, 1e6)
                if lo is not None and hi is not None and 0 < lo < hi:
                    cfg.band = (lo, hi)
            gate = _number(patch_cfg.get("gate_quantile"), 0.0, 0.95)
            if gate is not None:
                cfg.selection.gate_quantile = gate

        config = self.config_dict()
        self.broadcast({"type": "config", "config": config})
        return config

    def recalibrate(self, node_id: int | None = None) -> None:
        for state in self.nodes.values():
            if node_id is None or state.node_id == node_id:
                state.recalibrate()
        self.broadcast({"type": "recalibrated", "node_id": node_id})
