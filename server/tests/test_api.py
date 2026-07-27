"""HTTP and WebSocket surface, driven through the real ASGI app."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from csi.api import create_app
from csi.config import Settings
from csi.downlink import decode_frame
from csi.protocol import encode_frame
from csi.synth import Scene, SceneConfig


@pytest.fixture
def client(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.web_dir = None
    settings.record = False
    settings.udp_port = 0  # let the kernel choose; the tests inject frames directly
    settings.metrics_hz = 50.0
    settings.ensure_dirs()

    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.hub = app.state.hub
        yield test_client


def feed(hub, n=200, **scene_kwargs):
    scene = Scene(SceneConfig(**scene_kwargs))
    sent = 0
    while sent < n:
        frame = scene.next_frame()
        if frame is None:
            continue
        hub.handle_datagram(encode_frame(frame), 1000.0)
        sent += 1


def test_status_reports_an_empty_system(client):
    body = client.get("/api/status").json()
    assert body["nodes"] == []
    assert body["counters"]["live_frames"] == 0
    assert body["config"]["breathing"]["window_s"] == 20.0


def test_status_reports_a_node_once_it_appears(client):
    feed(client.hub, 100)
    body = client.get("/api/status").json()

    assert body["nodes"][0]["node_id"] == 1
    assert body["nodes"][0]["n_sub"] == 64
    assert body["layout"]["name"] == "HT20"


def test_healthz(client):
    assert client.get("/api/healthz").json()["ok"] is True


def test_config_patch_round_trips(client):
    patched = client.patch("/api/config", json={"breathing": {"window_s": 15.0}}).json()
    assert patched["breathing"]["window_s"] == 15.0
    assert client.get("/api/config").json()["breathing"]["window_s"] == 15.0


def test_recording_lifecycle(client):
    started = client.post("/api/sessions", json={"label": "walking", "notes": "arm wave"}).json()
    session_id = started["session"]["id"]
    assert started["session"]["active"] is True

    feed(client.hub, 300)
    stopped = client.post("/api/recording/stop").json()
    assert stopped["session"]["frames"] == 300

    listing = client.get("/api/sessions").json()
    assert [s["id"] for s in listing["sessions"]] == [session_id]
    assert "empty-room" in listing["labels"]


def test_cannot_delete_the_session_being_recorded(client):
    started = client.post("/api/sessions", json={"label": "empty-room"}).json()
    response = client.delete(f"/api/sessions/{started['session']['id']}")
    assert response.status_code == 409


def test_delete_removes_the_files(client):
    started = client.post("/api/sessions", json={"label": "empty-room"}).json()
    session_id = started["session"]["id"]
    feed(client.hub, 50)
    client.post("/api/recording/stop")

    path = client.hub.sessions.recordings_dir if False else client.hub.sessions.directory
    assert (path / f"{session_id}.csi").exists()

    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert not (path / f"{session_id}.csi").exists()
    assert client.get("/api/sessions").json()["sessions"] == []


def test_edit_session_label(client):
    started = client.post("/api/sessions", json={"label": "unlabelled"}).json()
    session_id = started["session"]["id"]
    client.post("/api/recording/stop")

    edited = client.patch(f"/api/sessions/{session_id}", json={"label": "seated-still"}).json()
    assert edited["session"]["label"] == "seated-still"


def test_rescan_repairs_metadata_after_an_unclean_shutdown(client):
    """The recorder flushes session metadata on a timer, so a hard kill leaves the file complete
    and the counts stale. Rescanning must recover them from the recording itself."""
    started = client.post("/api/sessions", json={"label": "overnight"}).json()
    session_id = started["session"]["id"]
    feed(client.hub, 400)
    client.post("/api/recording/stop")

    session = client.hub.sessions.get(session_id)
    session.frames = 0
    session.last_t_us = None
    client.hub.sessions.update(session)

    rescanned = client.post(f"/api/sessions/{session_id}/rescan").json()
    assert rescanned["session"]["frames"] == 400
    assert rescanned["bad_records"] == 0


def test_replay_endpoints(client):
    client.post("/api/sessions", json={"label": "empty-room"})
    feed(client.hub, 400)
    session = client.post("/api/recording/stop").json()["session"]

    started = client.post(
        f"/api/sessions/{session['id']}/replay", json={"speed": 0.0}
    ).json()
    assert started["replay"]["path"].endswith(".csi")

    client.post("/api/replay/stop")
    assert client.get("/api/status").json()["replay"] is None


def test_replay_of_a_missing_session_is_404(client):
    assert client.post("/api/sessions/nope/replay", json={}).status_code == 404


def test_replay_control_without_a_replay_is_409(client):
    assert client.post("/api/replay/control", json={"action": "pause"}).status_code == 409


def test_recalibrate(client):
    feed(client.hub, 100)
    assert client.post("/api/recalibrate", json={}).json()["ok"] is True


# -- websocket ---------------------------------------------------------------------------


def test_websocket_greets_with_a_snapshot(client):
    with client.websocket_connect("/ws") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        assert "config" in hello
        assert "nodes" in hello


def test_websocket_delivers_binary_frames(client):
    with client.websocket_connect("/ws") as ws:
        json.loads(ws.receive_text())  # hello
        feed(client.hub, 3)

        meta, amp = decode_frame(ws.receive_bytes())
        assert meta["node_id"] == 1
        assert amp.size == 64


def test_websocket_subscription_filters_frames(client):
    with client.websocket_connect("/ws") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "subscribe", "nodes": [1], "decimate": 2}))
        # The subscribe is handled on the server's receive task; give it a round trip.
        ws.send_text(json.dumps({"type": "ping", "t": 1}))
        assert json.loads(ws.receive_text())["type"] == "pong"

        feed(client.hub, 4)
        first, _ = decode_frame(ws.receive_bytes())
        second, _ = decode_frame(ws.receive_bytes())
        assert second["seq"] - first["seq"] == 2


def test_websocket_config_message_reaches_the_hub(client):
    with client.websocket_connect("/ws") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "config", "config": {"breathing": {"window_s": 8.0}}}))
        ws.send_text(json.dumps({"type": "ping", "t": 2}))

        while True:
            message = json.loads(ws.receive_text())
            if message["type"] == "pong":
                break

        assert client.get("/api/config").json()["breathing"]["window_s"] == 8.0


def test_websocket_ignores_malformed_client_messages(client):
    with client.websocket_connect("/ws") as ws:
        json.loads(ws.receive_text())
        ws.send_text("not json at all")
        ws.send_text(json.dumps(["a list, not an object"]))
        ws.send_text(json.dumps({"type": "ping", "t": 3}))

        assert json.loads(ws.receive_text())["type"] == "pong"
