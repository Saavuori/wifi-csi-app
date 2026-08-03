"""Node control: the desired/applied loop and the WiFi overview endpoints."""

from __future__ import annotations

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
