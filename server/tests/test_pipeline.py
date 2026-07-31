"""Ingest, recording, replay, and the hub that ties them together.

The headline test is `test_replay_is_byte_identical_to_what_was_recorded` — Phase 2's exit
criterion, and the property the entire "develop against recordings" workflow rests on.
"""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

from csi.config import Settings
from csi.downlink import decode_frame
from csi.hub import Client, Hub
from csi.nodes import NodeHealth
from csi.protocol import encode_frame, parse_frame
from csi.recorder import Recorder, read_index, scan_recording
from csi.ring import FrameRing
from csi.sessions import SessionStore
from csi.synth import Scene, SceneConfig


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path
    s.web_dir = None
    s.record = False
    s.history_s = 30.0
    s.ensure_dirs()
    return s


def datagrams(n: int = 300, **scene_kwargs) -> list[bytes]:
    scene = Scene(SceneConfig(**scene_kwargs))
    out = []
    while len(out) < n:
        frame = scene.next_frame()
        if frame is not None:
            out.append(encode_frame(frame))
    return out


# -- recording -----------------------------------------------------------------------------


def test_recording_round_trips_and_indexes(settings):
    store = SessionStore(settings.recordings_dir)
    session = store.create("empty-room")
    recorder = Recorder(store, session)

    written = datagrams(600)
    for raw in written:
        recorder.write(raw, parse_frame(raw))
    recorder.close()

    path = store.file_for(session)
    summary = scan_recording(path)
    assert summary["frames"] == len(written)
    assert summary["bad"] == 0
    assert summary["node_ids"] == [1]

    index = read_index(path)
    assert len(index) == 600 // 256 + 1
    assert index[0][1] == len(b"CSIREC01"), "first entry points just past the file header"
    assert [t for t, _ in index] == sorted(t for t, _ in index)


def test_session_metadata_tracks_the_stream(settings):
    store = SessionStore(settings.recordings_dir)
    session = store.create("walking", notes="waving an arm")
    recorder = Recorder(store, session)

    for raw in datagrams(120):
        recorder.write(raw, parse_frame(raw))
    recorder.close()

    reloaded = SessionStore(settings.recordings_dir).get(session.id)
    assert reloaded is not None
    assert reloaded.label == "walking"
    assert reloaded.notes == "waving an arm"
    assert reloaded.frames == 120
    assert reloaded.n_sub == 64
    assert reloaded.duration_s > 0
    assert reloaded.ended_at is not None


def test_session_store_survives_a_corrupt_index(settings):
    store = SessionStore(settings.recordings_dir)
    store.create("empty-room")
    store.path.write_text("{ this is not json")

    recovered = SessionStore(settings.recordings_dir)
    assert recovered.sorted() == []
    assert list(settings.recordings_dir.glob("*.corrupt-*.json"))


# -- replay ---------------------------------------------------------------------------------


async def test_replay_is_byte_identical_to_what_was_recorded(settings):
    """Phase 2's exit criterion. Not "equivalent", not "close enough to analyse" — the same
    bytes, so the replayer cannot silently normalize away a bug that live data would hit."""
    from csi.replay import Replayer

    store = SessionStore(settings.recordings_dir)
    session = store.create("empty-room")
    recorder = Recorder(store, session)
    written = datagrams(500)
    for raw in written:
        recorder.write(raw, parse_frame(raw))
    recorder.close()

    seen: list[bytes] = []
    replayer = Replayer(store.file_for(session), lambda d, t: seen.append(d), speed=0.0)
    await replayer.run()

    assert seen == written


async def test_replay_speed_scales_wall_clock(settings):
    from csi.replay import Replayer

    store = SessionStore(settings.recordings_dir)
    session = store.create("empty-room")
    recorder = Recorder(store, session)
    # 2 seconds of device time at 80 Hz.
    for raw in datagrams(160):
        recorder.write(raw, parse_frame(raw))
    recorder.close()

    async def elapsed_for(speed: float) -> float:
        replayer = Replayer(store.file_for(session), lambda d, t: None, speed=speed)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await replayer.run()
        return loop.time() - start

    fast = await elapsed_for(8.0)
    assert fast < 1.0, "8x replay of 2 s of data should take well under a second"


async def test_replay_seek_starts_at_the_requested_time(settings):
    from csi.replay import Replayer

    store = SessionStore(settings.recordings_dir)
    session = store.create("overnight")
    recorder = Recorder(store, session)
    written = datagrams(1200)
    for raw in written:
        recorder.write(raw, parse_frame(raw))
    recorder.close()

    target = parse_frame(written[800]).timestamp
    seen: list[bytes] = []
    replayer = Replayer(store.file_for(session), lambda d, t: seen.append(d), speed=0.0)
    replayer.seek(target)
    await replayer.run()

    assert seen[0] == written[800]
    assert len(seen) == len(written) - 800


async def test_replay_of_a_missing_index_still_works(settings):
    """The index is an optimization. Deleting it must cost speed, not correctness."""
    from csi.replay import Replayer

    store = SessionStore(settings.recordings_dir)
    session = store.create("empty-room")
    recorder = Recorder(store, session)
    written = datagrams(400)
    for raw in written:
        recorder.write(raw, parse_frame(raw))
    recorder.close()

    path = store.file_for(session)
    path.with_suffix(path.suffix + ".idx").unlink()

    target = parse_frame(written[300]).timestamp
    seen: list[bytes] = []
    replayer = Replayer(path, lambda d, t: seen.append(d), speed=0.0)
    replayer.seek(target)
    await replayer.run()

    assert seen[0] == written[300]


# -- node health ------------------------------------------------------------------------------


def test_sequence_gaps_are_counted():
    health = NodeHealth(node_id=1)
    scene = Scene(SceneConfig())

    for _ in range(200):
        frame = scene.next_frame()
        if frame is None:
            continue
        if 50 <= frame.seq < 55:
            continue  # five frames lost in flight
        health.observe(frame)

    assert health.gaps == 1
    assert health.missing == 5
    assert 0.02 < health.loss_rate < 0.04


def test_rate_is_measured_from_device_timestamps():
    health = NodeHealth(node_id=1)
    scene = Scene(SceneConfig(rate_hz=80.0, jitter_us=0))
    for _ in range(300):
        frame = scene.next_frame()
        if frame is not None:
            health.observe(frame)

    assert health.rate_hz == pytest.approx(80.0, rel=0.02)
    assert health.jitter_ms < 0.1


def test_jitter_shows_up_in_the_health_report():
    health = NodeHealth(node_id=1)
    scene = Scene(SceneConfig(rate_hz=80.0, jitter_us=3000))
    for _ in range(300):
        frame = scene.next_frame()
        if frame is not None:
            health.observe(frame)

    assert health.jitter_ms > 1.0


def test_reboot_is_detected_and_reported():
    """A reboot resets both the sequence counter and the device clock. Treating it as a gap of
    four billion frames would poison every statistic at once."""
    health = NodeHealth(node_id=1)
    scene = Scene(SceneConfig())
    for _ in range(100):
        frame = scene.next_frame()
        if frame is not None:
            health.observe(frame)

    rebooted = Scene(SceneConfig()).next_frame()
    assert health.observe(rebooted) is True
    assert health.reboots == 1
    assert health.missing < 10


def test_duplicate_frames_are_not_counted_as_gaps():
    health = NodeHealth(node_id=1)
    scene = Scene(SceneConfig())
    frame = scene.next_frame()
    health.observe(frame)
    health.observe(frame)

    assert health.reorders == 1
    assert health.gaps == 0


# -- the ring ----------------------------------------------------------------------------------


def test_ring_windows_are_selected_by_time_not_frame_count():
    """A 20 s window that silently holds 14 s of data produces a confident wrong BPM, so the
    window has to be the duration it claims to be."""
    from csi.dsp.preprocess import Preprocessor

    ring = FrameRing(64, 4000)
    pre = Preprocessor()
    scene = Scene(SceneConfig(rate_hz=80.0, drop_rate=0.3))
    for _ in range(2000):
        frame = scene.next_frame()
        if frame is not None:
            ring.push(pre.process(frame))

    window = ring.seconds(5.0)
    assert window.duration_s == pytest.approx(5.0, abs=0.1)
    assert len(window) < 5.0 * 80 * 0.9, "heavy loss must show as fewer samples, not a short window"


def test_ring_wraps_without_reordering():
    from csi.dsp.preprocess import Preprocessor

    ring = FrameRing(64, 100)
    pre = Preprocessor()
    scene = Scene(SceneConfig(jitter_us=0))
    for _ in range(350):
        frame = scene.next_frame()
        if frame is not None:
            ring.push(pre.process(frame))

    assert len(ring) == 100
    window = ring.latest(100)
    assert list(window.t_us) == sorted(window.t_us)


def test_ring_rejects_a_bandwidth_change():
    from csi.dsp.preprocess import Preprocessor

    ring = FrameRing(64, 100)
    pre = Preprocessor()
    wide = Scene(SceneConfig(n_sub=128)).next_frame()
    with pytest.raises(ValueError, match="n_sub"):
        ring.push(pre.process(wide))


# -- the hub ------------------------------------------------------------------------------------


async def test_hub_routes_a_frame_all_the_way_to_a_client(settings):
    hub = Hub(settings)
    client = Client()
    hub.add_client(client)

    raw = datagrams(1)[0]
    hub.handle_datagram(raw, 1000.0)

    messages = [client.queue.get_nowait() for _ in range(client.queue.qsize())]
    binary = [m for m in messages if isinstance(m, bytes)]
    assert len(binary) == 1

    meta, amp = decode_frame(binary[0])
    assert meta["node_id"] == 1
    assert amp.size == 64
    assert hub.live_frames == 1


async def test_hub_counts_malformed_datagrams_instead_of_dying(settings):
    hub = Hub(settings)
    hub.handle_datagram(b"garbage", 1000.0)
    hub.handle_datagram(b"", 1000.0)
    hub.handle_datagram(datagrams(1)[0], 1000.0)

    assert hub.bad_packets == 2
    assert hub.live_frames == 1


async def test_hub_drops_frames_for_a_stalled_client_rather_than_blocking(settings):
    """A browser tab in the background must degrade itself, never apply backpressure to ingest."""
    hub = Hub(settings)
    client = Client()
    hub.add_client(client)

    for raw in datagrams(1000):
        hub.handle_datagram(raw, 1000.0)

    assert client.dropped > 0
    assert client.queue.qsize() <= client.queue.maxsize
    assert hub.live_frames == 1000


async def test_client_subscription_filters_by_node(settings):
    hub = Hub(settings)
    client = Client(nodes={7})
    hub.add_client(client)
    while not client.queue.empty():
        client.queue.get_nowait()

    for raw in datagrams(5):  # node_id 1
        hub.handle_datagram(raw, 1000.0)

    assert client.queue.empty()


async def test_client_decimation(settings):
    hub = Hub(settings)
    client = Client(decimate=4)
    hub.add_client(client)
    while not client.queue.empty():
        client.queue.get_nowait()

    for raw in datagrams(40):
        hub.handle_datagram(raw, 1000.0)

    assert client.queue.qsize() == 10


async def test_live_frames_are_suppressed_while_a_replay_runs(settings):
    """A replay owns the node IDs while it runs; mixing a recorded room with the live one into
    a single ring would interleave two different scenes."""
    hub = Hub(settings)
    hub.start_recording("empty-room")
    for raw in datagrams(300):
        hub.handle_datagram(raw, 1000.0)
    session = hub.stop_recording()

    await hub.start_replay(session["id"], speed=0.0)
    hub.handle_datagram(datagrams(1)[0], 2000.0)
    assert hub.suppressed_live >= 1

    await hub.stop_replay()
    before = hub.live_frames
    hub.handle_datagram(datagrams(1)[0], 3000.0)
    assert hub.live_frames == before + 1


async def test_hub_computes_metrics_over_a_recorded_scene(settings):
    hub = Hub(settings)
    scene = Scene(SceneConfig(breathing_bpm=15.0, rate_hz=80.0))
    for _ in range(int(45 * 80)):
        frame = scene.next_frame()
        if frame is not None:
            hub.handle_datagram(encode_frame(frame), 1000.0)

    state = hub.nodes[1]
    metrics = hub.compute_metrics(state, hub.history_for(state))
    assert metrics is not None
    assert metrics["node_id"] == 1
    assert metrics["presence"]["state"] in ("calibrating", "absent", "present")
    assert metrics["breathing"]["bpm"] == pytest.approx(15.0, abs=2.0)
    assert "breathing_snr_db" in metrics["placement"]
    assert metrics["rate_hz"] == pytest.approx(80.0, rel=0.05)
    assert len(metrics["variance"]["values"]) == len(metrics["variance"]["subcarriers"])


# -- analysis off the event loop -------------------------------------------------------------


async def test_the_dsp_runs_off_the_event_loop(settings):
    """The point of the arrangement: an SVD and two filtfilts per node per pass must not sit on
    the loop that is servicing UDP and the WebSocket writers."""
    hub = Hub(settings)
    for raw in datagrams(2000):
        hub.handle_datagram(raw, 1000.0)

    ran_on: list[int] = []
    original = hub.compute_metrics

    def spy(*args):
        ran_on.append(threading.get_ident())
        return original(*args)

    hub.compute_metrics = spy
    await hub._emit_metrics()

    assert ran_on, "the analysis did not run at all"
    assert ran_on[0] != threading.get_ident()
    assert hub.nodes[1].last_metrics["node_id"] == 1
    assert hub.nodes[1].analyzing is False


def test_a_snapshot_is_frozen_against_further_ingest():
    """What `history_for` buys. If the snapshot were a view onto the ring, a window handed to a
    worker thread would eventually be spliced across the write pointer — two different moments
    in one array, joined at whatever index the writer had reached."""
    from csi.dsp.preprocess import Preprocessor

    ring = FrameRing(64, 200)
    pre = Preprocessor()
    scene = Scene(SceneConfig(jitter_us=0))
    for _ in range(200):
        frame = scene.next_frame()
        if frame is not None:
            ring.push(pre.process(frame))

    snapshot = ring.seconds(60.0)
    t_before = snapshot.t_us.copy()
    amp_before = snapshot.amp.copy()

    # Overwrite every slot the snapshot came from, twice over.
    for _ in range(400):
        frame = scene.next_frame()
        if frame is not None:
            ring.push(pre.process(frame))

    assert np.array_equal(snapshot.t_us, t_before)
    assert np.array_equal(snapshot.amp, amp_before, equal_nan=True)
    assert snapshot.t_us[-1] < ring.seconds(60.0).t_us[-1], "the ring did move on"


def test_a_snapshot_windows_the_same_way_the_ring_does():
    """The substitution the analyzers cannot be allowed to notice: they call `.seconds()` on
    whatever history they are given, and must get the same answer either way."""
    from csi.dsp.preprocess import Preprocessor

    ring = FrameRing(64, 4000)
    pre = Preprocessor()
    scene = Scene(SceneConfig(rate_hz=80.0, drop_rate=0.2))
    for _ in range(2000):
        frame = scene.next_frame()
        if frame is not None:
            ring.push(pre.process(frame))

    snapshot = ring.seconds(20.0)
    assert snapshot.n_sub == ring.n_sub
    for duration in (0.5, 2.0, 5.0, 20.0):
        direct = ring.seconds(duration)
        via = snapshot.seconds(duration)
        assert len(via) == len(direct)
        assert np.array_equal(via.t_us, direct.t_us)
        assert np.array_equal(via.amp, direct.amp, equal_nan=True)


def test_a_reset_during_analysis_is_deferred_then_applied(settings):
    """Clearing the ring or rewinding the detector while a worker is mid-window is the race the
    threading introduces. The loop waits for the pass to end instead."""
    hub = Hub(settings)
    for raw in datagrams(500):
        hub.handle_datagram(raw, 1000.0)
    state = hub.nodes[1]

    state.analyzing = True
    state.reset()
    assert len(state.ring) == 500, "the ring must survive until the analysis has finished"

    state.analyzing = False
    assert state.take_pending() is True, "the in-flight result has to be reported stale"
    assert len(state.ring) == 0
    assert state.take_pending() is False


@pytest.mark.parametrize("order", [("recalibrate", "reset"), ("reset", "recalibrate")])
def test_a_deferred_reset_outranks_a_deferred_recalibrate(settings, order):
    hub = Hub(settings)
    for raw in datagrams(500):
        hub.handle_datagram(raw, 1000.0)
    state = hub.nodes[1]

    state.analyzing = True
    for op in order:
        getattr(state, op)()
    state.analyzing = False
    state.take_pending()

    assert len(state.ring) == 0, "the weaker request must not swallow the stronger one"


async def test_a_node_appearing_mid_pass_does_not_break_the_loop(settings):
    """`_emit_metrics` awaits between nodes, so ingest can add one while it is iterating."""
    hub = Hub(settings)
    for raw in datagrams(600):
        hub.handle_datagram(raw, 1000.0)

    original = hub.compute_metrics

    def spy(*args):
        for raw in datagrams(20, node_id=7):
            hub.handle_datagram(raw, 1000.0)
        return original(*args)

    hub.compute_metrics = spy
    await hub._emit_metrics()  # must not raise "dictionary changed size during iteration"

    assert set(hub.nodes) == {1, 7}


async def test_node_reboot_clears_history(settings):
    hub = Hub(settings)
    for raw in datagrams(500):
        hub.handle_datagram(raw, 1000.0)
    assert len(hub.nodes[1].ring) == 500

    hub.handle_datagram(datagrams(1)[0], 1001.0)  # a fresh scene: seq and clock reset
    assert len(hub.nodes[1].ring) == 1
    assert hub.nodes[1].health.reboots == 1


async def test_config_patch_applies_and_ignores_nonsense(settings):
    hub = Hub(settings)
    updated = hub.update_config(
        {
            "breathing": {"window_s": 12.0},
            "presence": {"enter_sigma": 9.0},
            "preprocess": {"norm_mode": "not-a-mode"},
            "nonexistent": {"whatever": 1},
        }
    )

    assert updated["breathing"]["window_s"] == 12.0
    assert updated["presence"]["enter_sigma"] == 9.0
    assert updated["preprocess"]["norm_mode"] == "hybrid", "an invalid mode must be ignored"


async def test_snapshot_describes_the_running_system(settings):
    hub = Hub(settings)
    for raw in datagrams(10):
        hub.handle_datagram(raw, 1000.0)

    snapshot = hub.snapshot()
    assert snapshot["nodes"][0]["node_id"] == 1
    assert snapshot["layout"]["name"] == "HT20"
    assert snapshot["counters"]["live_frames"] == 10
    assert snapshot["config"]["breathing"]["window_s"] > 0
