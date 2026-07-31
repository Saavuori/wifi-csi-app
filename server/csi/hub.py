"""The centre of the server: one datagram in, everything else out.

Every frame — live from UDP or replayed from disk — enters through `handle_datagram` and takes
exactly the same route: parse, health accounting, record, preprocess, push to the node's ring,
fan out to clients. Analysis runs on a separate slow timer over the rings, because presence and
breathing want a window, not a frame, and there is no reason to run an SVD at 80 Hz.

Keeping live and replay on one path is not a tidiness preference. It is the thing that makes a
recording a valid substitute for the room, which is what lets the detector be developed at a
desk instead of on the floor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
from .ring import FrameRing
from .sessions import SessionStore

log = logging.getLogger("csi.hub")

# A client that cannot keep up gets its oldest frames dropped rather than blocking ingest. A
# second of history is plenty of slack for a browser tab that briefly went to the background.
CLIENT_QUEUE = 256


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

    def ensure_ring(self, n_sub: int) -> FrameRing:
        if self.ring is None or self.ring.n_sub != n_sub:
            self.ring = FrameRing(n_sub, self.settings.ring_capacity())
        return self.ring

    def reset(self) -> None:
        """Forget history. Used on node reboot and when a replay starts or seeks — carrying a
        window across a discontinuity puts a step change into every analysis at once."""
        self.pre.reset()
        self.presence.recalibrate()
        if self.ring is not None:
            self.ring.clear()


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
        self._stopping = True
        await self.stop_replay()
        if self._metrics_task:
            self._metrics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._metrics_task
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
        state.ensure_ring(frame.n_sub).push(processed)
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
        if self.replayer is None:
            return
        self.replayer.stop()
        if self._replay_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._replay_task, timeout=2.0)
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
                self._emit_metrics()
            except Exception:  # a bad window must not kill the loop for the rest of the session
                log.exception("metrics computation failed")

    def _emit_metrics(self) -> None:
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
        for state in self.nodes.values():
            metrics = self.compute_metrics(state)
            if metrics is None:
                continue
            state.last_metrics = metrics
            if self.clients:
                self.broadcast({"type": "metrics", "t": now, **metrics})
        if self.clients:
            self.broadcast({"type": "nodes", "nodes": self.node_report(now)})

    def compute_metrics(self, state: NodeState) -> dict | None:
        ring = state.ring
        if ring is None or len(ring) < 16:
            return None

        out: dict = {"node_id": state.node_id, "n_sub": ring.n_sub}

        presence = state.presence.update(ring)
        if presence is not None:
            out["presence"] = presence.as_dict()

        breathing = state.breathing.estimate(ring)
        if breathing is not None:
            out["breathing"] = breathing.as_dict()
        elif state.breathing.last_rejection:
            # Named rather than silent. A breathing panel that just goes blank looks like a
            # broken server; one that says the link had a two-second hole in it points at the
            # actual problem, which on a shared access point is usually the network being busy.
            out["breathing_rejected"] = state.breathing.last_rejection

        if self.settings.heart.window_s > 0 and len(ring) > 64:
            heart = state.heart.estimate(ring)
            if heart is not None:
                out["heart"] = heart.as_dict()
            elif state.heart.last_rejection:
                out["heart_rejected"] = state.heart.last_rejection

        # The placement tuner wants one number that responds immediately to the node being
        # moved, so it runs on a short window regardless of the breathing window setting.
        tuner_window = ring.seconds(min(20.0, self.settings.breathing.window_s))
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

        window = ring.seconds(self.settings.presence.window_s)
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
        """Apply a partial config update from the UI. Unknown keys are ignored on purpose —
        the client should not be able to crash the server with a typo."""
        s = self.settings

        pre = patch.get("preprocess", {})
        if "norm_mode" in pre and pre["norm_mode"] in ("hybrid", "rssi", "rms", "none"):
            s.preprocess.norm_mode = pre["norm_mode"]
        for key in ("drop_pilots", "hampel_enabled"):
            if key in pre:
                setattr(s.preprocess, key, bool(pre[key]))
        for key in ("agc_step_db", "agc_uniformity"):
            if key in pre:
                setattr(s.preprocess, key, float(pre[key]))

        pres = patch.get("presence", {})
        for key in ("window_s", "calibration_s", "enter_sigma", "exit_sigma", "debounce_s"):
            if key in pres:
                setattr(s.presence, key, float(pres[key]))
        if "use_pca" in pres:
            s.presence.use_pca = bool(pres["use_pca"])
        if "gate_quantile" in pres:
            s.presence.selection.gate_quantile = float(pres["gate_quantile"])
        if "top_k" in pres:
            s.presence.selection.top_k = int(pres["top_k"])

        for name, cfg in (("breathing", s.breathing), ("heart", s.heart)):
            patch_cfg = patch.get(name, {})
            if "window_s" in patch_cfg:
                cfg.window_s = float(patch_cfg["window_s"])
            if "n_subcarriers" in patch_cfg:
                cfg.n_subcarriers = int(patch_cfg["n_subcarriers"])
            if "band" in patch_cfg and len(patch_cfg["band"]) == 2:
                lo, hi = (float(v) for v in patch_cfg["band"])
                if 0 < lo < hi:
                    cfg.band = (lo, hi)
            if "gate_quantile" in patch_cfg:
                cfg.selection.gate_quantile = float(patch_cfg["gate_quantile"])

        config = self.config_dict()
        self.broadcast({"type": "config", "config": config})
        return config

    def recalibrate(self, node_id: int | None = None) -> None:
        for state in self.nodes.values():
            if node_id is None or state.node_id == node_id:
                state.presence.recalibrate()
        self.broadcast({"type": "recalibrated", "node_id": node_id})
