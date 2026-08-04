"""Node control: the desired/applied loop and the WiFi overview endpoints."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from csi.api import create_app
from csi.config import Settings
from csi.nodecontrol import NodeControlStore
from csi.protocol import encode_frame
from csi.synth import Scene, SceneConfig


@pytest.fixture
def client(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.web_dir = None
    settings.record = False
    settings.udp_port = 0
    settings.ensure_dirs()

    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.hub = app.state.hub
        yield test_client


def feed(hub, n=20):
    scene = Scene(SceneConfig())
    sent = 0
    while sent < n:
        frame = scene.next_frame()
        if frame is None:
            continue
        # The synth does not model a transmitter; the overview only lists MACs it has heard,
        # so give the frames one.
        frame.src_mac = b"\xaa\xbb\xcc\xdd\xee\xff"
        hub.handle_datagram(encode_frame(frame), 1000.0 + sent / 80.0)
        sent += 1


# -- the store ----------------------------------------------------------------------------


def test_defaults_are_auto(tmp_path):
    store = NodeControlStore(tmp_path / "control.json")
    entry = store.get(20)
    assert entry["desired"] == {"channel": "auto", "stimulus": "auto"}
    assert entry["revision"] == 0
    assert entry["applied"] is None


def test_patch_bumps_revision_and_persists(tmp_path):
    path = tmp_path / "control.json"
    store = NodeControlStore(path)
    entry = store.patch(20, {"channel": "36/80", "stimulus": "off"})
    assert entry["desired"] == {"channel": "36/80", "stimulus": "off"}
    assert entry["revision"] == 1

    reloaded = NodeControlStore(path).get(20)
    assert reloaded["desired"] == {"channel": "36/80", "stimulus": "off"}
    assert reloaded["revision"] == 1


def test_patch_rejects_garbage_without_bumping(tmp_path):
    store = NodeControlStore(tmp_path / "control.json")
    entry = store.patch(20, {"channel": "999/80", "stimulus": "loud", "extra": True})
    assert entry["desired"] == {"channel": "auto", "stimulus": "auto"}
    assert entry["revision"] == 0

    # A no-op patch (same values) must not bump either, or every poll cycle would look like a
    # pending change to the node.
    store.patch(20, {"stimulus": "always"})
    entry = store.patch(20, {"stimulus": "always"})
    assert entry["revision"] == 1


def test_report_is_runtime_only(tmp_path):
    path = tmp_path / "control.json"
    store = NodeControlStore(path)
    store.patch(20, {"channel": "36/80"})
    store.report(20, {"revision": 1, "applied": {"channel": "36/80", "stimulus": "auto"}})

    assert store.get(20)["applied"] == {"channel": "36/80", "stimulus": "auto"}
    assert NodeControlStore(path).get(20)["applied"] is None


def test_scan_request_and_results(tmp_path):
    store = NodeControlStore(tmp_path / "control.json")
    assert store.request_scan(20)["scan_rev"] == 1
    aps = [{"bssid": "aa:bb:cc:dd:ee:ff", "ssid": "home", "channel": 36}]
    entry = store.report(20, {"revision": 0, "scan_rev": 1, "scan": {"aps": aps}})
    assert entry["scan"]["aps"] == aps
    assert entry["reported_scan_rev"] == 1


# -- the HTTP surface ---------------------------------------------------------------------


def test_control_roundtrip_over_http(client):
    body = client.patch("/api/nodes/20/control", json={"stimulus": "always"}).json()
    assert body["desired"]["stimulus"] == "always"
    assert body["revision"] == 1

    client.post(
        "/api/nodes/20/control/report",
        json={"revision": 1, "applied": {"channel": "6/20", "stimulus": "always"}},
    )
    body = client.get("/api/nodes/20/control").json()
    assert body["applied"]["stimulus"] == "always"
    assert body["reported_rev"] == 1


def test_control_rejects_reserved_node_ids(client):
    assert client.get("/api/nodes/0/control").status_code == 400
    assert client.patch("/api/nodes/255/control", json={}).status_code == 400


def test_wifi_reports_transmitters_heard(client):
    feed(client.hub, 20)
    body = client.get("/api/wifi").json()
    node = next(n for n in body["nodes"] if n["node_id"] == 1)
    assert len(node["transmitters"]) == 1
    transmitter = node["transmitters"][0]
    assert transmitter["frames"] == 20
    assert transmitter["mac"].count(":") == 5


def test_wifi_includes_configured_but_silent_nodes(client):
    client.patch("/api/nodes/77/control", json={"channel": "36/80"})
    body = client.get("/api/wifi").json()
    node = next(n for n in body["nodes"] if n["node_id"] == 77)
    assert node["desired"]["channel"] == "36/80"
    assert node["transmitters"] == []


# -- node health reports an absent noise floor honestly ------------------------------------


def _frame(rssi: int, noise: int):
    import numpy as np

    from csi.protocol import Frame

    return Frame(
        node_id=1, seq=0, timestamp=0, rssi=rssi, noise_floor=noise, channel=6,
        sec_channel=0, n_sub=64, data=np.zeros(128, dtype=np.int8),
    )


def test_snr_is_none_when_the_driver_reports_no_noise_floor():
    """brcmfmac leaves the column empty; the node normalizes that to 0. rssi - 0 is not an SNR."""
    from csi.nodes import NodeHealth

    health = NodeHealth(node_id=1)
    health.observe(_frame(rssi=-49, noise=0), now=1000.0)
    assert health.as_dict()["snr_db"] is None


def test_snr_is_reported_when_the_noise_floor_is_real():
    from csi.nodes import NodeHealth

    health = NodeHealth(node_id=1)
    health.observe(_frame(rssi=-49, noise=-92), now=1000.0)
    assert health.as_dict()["snr_db"] == 43


# -- recording retention -------------------------------------------------------------------


def _write_session(store, label: str, size: int):
    session = store.create(label)
    path = store.file_for(session)
    path.write_bytes(b"\0" * size)
    (path.parent / (session.path + ".idx")).write_bytes(b"\0" * 16)
    return session


def test_prune_is_a_no_op_under_budget(tmp_path):
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    _write_session(store, "a", 1000)
    assert store.prune(10_000) == []
    assert len(store.sorted()) == 1


def test_prune_removes_oldest_first(tmp_path):
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    oldest = _write_session(store, "oldest", 4000)
    middle = _write_session(store, "middle", 4000)
    newest = _write_session(store, "newest", 4000)
    # Creation order is the age order; make it explicit so the test does not rely on clock ties.
    oldest.started_at, middle.started_at, newest.started_at = 100.0, 200.0, 300.0
    for s in (oldest, middle, newest):
        store.update(s)

    removed = store.prune(9000)
    assert removed == [oldest.id]
    assert {s.id for s in store.sorted()} == {middle.id, newest.id}


def test_prune_never_deletes_the_session_being_written(tmp_path):
    """Deleting the file under the recorder loses the session someone is actually watching."""
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    old = _write_session(store, "old", 5000)
    active = _write_session(store, "active", 5000)
    old.started_at, active.started_at = 100.0, 200.0
    for s in (old, active):
        store.update(s)

    removed = store.prune(1000, keep=active)
    assert active.id not in removed
    assert store.get(active.id) is not None


def test_prune_disabled_by_a_zero_budget(tmp_path):
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    _write_session(store, "a", 5000)
    assert store.prune(0) == []
    assert len(store.sorted()) == 1


def test_total_bytes_reads_the_disk_not_the_metadata(tmp_path):
    """Session.bytes is flushed periodically and lags; a retention decision cannot use it."""
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    session = _write_session(store, "a", 2048)
    session.bytes = 0  # metadata deliberately stale
    store.update(session)
    assert store.total_bytes() == 2048


def _finished(store, label: str, size: int, started_at: float, ended_at: float):
    session = _write_session(store, label, size)
    session.started_at, session.ended_at = started_at, ended_at
    session.frames = 1000
    store.update(session)
    return session


def test_age_prune_deletes_only_what_is_wholly_outside_the_window(tmp_path):
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    now = 100_000.0
    day = 24 * 3600.0
    stale = _finished(store, "stale", 1000, now - 3 * day, now - 2 * day)
    recent = _finished(store, "recent", 1000, now - 3600.0, now - 60.0)

    removed = store.prune_older_than(day, now=now)

    assert removed == [stale.id]
    assert store.get(recent.id) is not None


def test_age_prune_measures_from_the_end_of_a_recording(tmp_path):
    """An overnight run is kept while any of it is still inside the window."""
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    now = 100_000.0
    day = 24 * 3600.0
    # Started 30 h ago, ended 22 h ago: outside the window by start, inside it by end.
    overnight = _finished(store, "overnight", 1000, now - 30 * 3600.0, now - 22 * 3600.0)

    assert store.prune_older_than(day, now=now) == []
    assert store.get(overnight.id) is not None


def test_age_prune_spares_the_session_being_written(tmp_path):
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    now = 100_000.0
    active = _write_session(store, "active", 1000)
    active.started_at, active.ended_at = now - 5 * 24 * 3600.0, None
    store.update(active)

    assert store.prune_older_than(24 * 3600.0, keep=active, now=now) == []
    assert store.get(active.id) is not None


def test_age_prune_expires_a_session_left_unfinished_by_a_crash(tmp_path):
    """No `ended_at` and not the one being written: it must not become immortal."""
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    now = 100_000.0
    orphan = _write_session(store, "orphan", 1000)
    orphan.started_at, orphan.ended_at = now - 5 * 24 * 3600.0, None
    store.update(orphan)

    assert store.prune_older_than(24 * 3600.0, now=now) == [orphan.id]


def test_age_prune_disabled_by_a_zero_window(tmp_path):
    from csi.sessions import SessionStore

    store = SessionStore(tmp_path)
    _finished(store, "ancient", 1000, 0.0, 1.0)
    assert store.prune_older_than(0, now=100_000.0) == []
    assert len(store.sorted()) == 1


def test_hub_applies_the_age_window(tmp_path):
    from csi.config import Settings
    from csi.hub import Hub

    settings = Settings()
    settings.data_dir = tmp_path
    settings.web_dir = None
    settings.record = False
    settings.max_age_h = 24.0
    settings.max_disk_gb = 0  # age alone must be enough to delete it
    settings.ensure_dirs()

    hub = Hub(settings)
    now = time.time()
    stale = _finished(hub.sessions, "stale", 1000, now - 4 * 86400.0, now - 3 * 86400.0)
    fresh = _finished(hub.sessions, "fresh", 1000, now - 3600.0, now - 60.0)

    hub._prune_recordings()

    assert hub.sessions.get(stale.id) is None
    assert hub.sessions.get(fresh.id) is not None


def test_hub_rolls_the_live_recording_once_it_is_old_enough(tmp_path):
    """Without rolling, the always-on session never finishes and so can never age out."""
    from csi.config import Settings
    from csi.hub import Hub

    settings = Settings()
    settings.data_dir = tmp_path
    settings.web_dir = None
    settings.record = False
    settings.roll_h = 1.0
    settings.ensure_dirs()

    hub = Hub(settings)
    hub.start_recording("live")
    first = hub.recorder.session
    first.frames = 1000
    first.started_at = time.time() - 3700.0

    hub._roll_recording()

    assert hub.recorder.session.id != first.id, "a new session must be open"
    assert hub.sessions.get(first.id).ended_at is not None, "the old one must be finished"


def test_roll_leaves_a_hand_started_capture_alone(tmp_path):
    """Splitting a capture someone is in the room making would break that recording."""
    from csi.config import Settings
    from csi.hub import Hub

    settings = Settings()
    settings.data_dir = tmp_path
    settings.web_dir = None
    settings.record = False
    settings.roll_h = 1.0
    settings.ensure_dirs()

    hub = Hub(settings)
    hub.start_recording("overnight")
    session = hub.recorder.session
    session.frames = 1000
    session.started_at = time.time() - 8 * 3600.0

    hub._roll_recording()

    assert hub.recorder.session.id == session.id


def test_roll_does_not_spend_a_session_on_an_idle_node(tmp_path):
    from csi.config import Settings
    from csi.hub import Hub

    settings = Settings()
    settings.data_dir = tmp_path
    settings.web_dir = None
    settings.record = False
    settings.roll_h = 1.0
    settings.ensure_dirs()

    hub = Hub(settings)
    hub.start_recording("live")
    session = hub.recorder.session
    session.frames = 0  # nothing arrived: no node, or a replay suppressing live frames
    session.started_at = time.time() - 8 * 3600.0

    hub._roll_recording()

    assert hub.recorder.session.id == session.id


def test_hub_prunes_but_keeps_the_active_recording(tmp_path):
    """The hub's own wiring, not just the store's: budget applied, active session spared."""
    from csi.config import Settings
    from csi.hub import Hub

    settings = Settings()
    settings.data_dir = tmp_path
    settings.web_dir = None
    settings.record = False
    settings.max_disk_gb = 8000 / 1024**3  # 8000 bytes, expressed in GB
    settings.max_age_h = 0  # isolate the size rule; the sessions here are deliberately ancient
    settings.ensure_dirs()

    hub = Hub(settings)
    old = _write_session(hub.sessions, "old", 5000)
    old.started_at = 100.0
    hub.sessions.update(old)
    hub.start_recording("live")
    active = hub.recorder.session
    hub.sessions.file_for(active).write_bytes(b"\0" * 5000)

    hub._prune_recordings()

    assert hub.sessions.get(active.id) is not None, "the session being written must survive"
    assert hub.sessions.get(old.id) is None, "the oldest must go"
