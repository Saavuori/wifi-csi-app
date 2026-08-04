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
    # 60 s, not the 20 s the literature suggests: see VitalsConfig.breathing().
    assert body["config"]["breathing"]["window_s"] == 60.0


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

    path = client.hub.sessions.directory
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


def _next_frame(ws):
    """The next binary CSI frame, skipping the JSON events that share the socket.

    Metrics, node health and config echoes are text messages on the same connection, so
    `receive_bytes()` fails with a KeyError whenever one happens to land between two frames.
    That made this test flake roughly one run in three.
    """
    while True:
        message = ws.receive()
        payload = message.get("bytes")
        if payload is not None:
            return decode_frame(payload)


def test_websocket_subscription_filters_frames(client):
    with client.websocket_connect("/ws") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "subscribe", "nodes": [1], "decimate": 2}))
        # The subscribe is handled on the server's receive task; give it a round trip.
        ws.send_text(json.dumps({"type": "ping", "t": 1}))
        assert json.loads(ws.receive_text())["type"] == "pong"

        feed(client.hub, 4)
        first, _ = _next_frame(ws)
        second, _ = _next_frame(ws)
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


BAD_PATCHES = [
    {"presence": {"window_s": "wide"}},
    {"presence": {"window_s": None}},
    {"presence": {"top_k": "eight"}},
    {"breathing": {"window_s": [20]}},
    {"breathing": {"n_subcarriers": {}}},
    {"breathing": {"band": "0.1-0.5"}},
    {"breathing": {"band": ["a", "b"]}},
    {"preprocess": {"agc_step_db": "loud"}},
    {"preprocess": 3},
]


@pytest.mark.parametrize("patch", BAD_PATCHES)
def test_config_patch_rejects_nonsense_without_a_500(client, patch):
    before = client.get("/api/config").json()
    response = client.patch("/api/config", json=patch)
    assert response.status_code == 200
    assert response.json() == before, "an unusable value must change nothing"


@pytest.mark.parametrize("patch", BAD_PATCHES)
def test_a_bad_config_message_does_not_close_the_websocket(client, patch):
    """The receive loop catches only WebSocketDisconnect and RuntimeError, so anything else
    escaping the handler costs the connection — and the browser reconnects with every view
    reset. A slider that sends its value as a string must not do that."""
    with client.websocket_connect("/ws") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "config", "config": patch}))
        ws.send_text(json.dumps({"type": "ping", "t": 7}))

        while True:
            message = json.loads(ws.receive_text())
            if message["type"] == "pong":
                assert message["t"] == 7
                break


def test_config_patch_rejects_out_of_range_values(client):
    before = client.get("/api/config").json()
    assert client.patch("/api/config", json={"presence": {"window_s": -5.0}}).json() == before
    assert client.patch("/api/config", json={"breathing": {"n_subcarriers": 0}}).json() == before
    # A checkbox posted into a numeric field: True is an int, and must not become 1.
    assert client.patch("/api/config", json={"breathing": {"window_s": True}}).json() == before


def test_config_patch_rejects_a_non_finite_number(client):
    """`json.loads` accepts the `NaN` and `Infinity` literals, so a client can send them even
    though they are not in the JSON grammar. A NaN window length poisons every reduction it
    reaches."""
    before = client.get("/api/config").json()
    response = client.patch(
        "/api/config",
        content='{"breathing": {"window_s": NaN}, "heart": {"gate_quantile": Infinity}}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == before


def test_recalibrate_rejects_a_non_integer_node_id(client):
    assert client.post("/api/recalibrate", json={"node_id": "all"}).status_code == 400
    assert client.post("/api/recalibrate", json={"node_id": 1}).status_code == 200
    assert client.post("/api/recalibrate", json={}).status_code == 200


def test_a_bad_subscribe_message_does_not_close_the_websocket(client):
    with client.websocket_connect("/ws") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "subscribe", "nodes": ["one", "two"]}))
        ws.send_text(json.dumps({"type": "recalibrate", "node_id": "everything"}))
        ws.send_text(json.dumps({"type": "ping", "t": 8}))

        while True:
            if json.loads(ws.receive_text())["type"] == "pong":
                break


# -- build identity ------------------------------------------------------------------------


def test_version_endpoint_reports_a_build(client):
    body = client.get("/api/version").json()
    assert set(body) == {"version", "commit", "built_at"}
    # Nothing is injected in a test run, so this is the honest fallback rather than a number.
    assert body["version"]


def test_status_carries_the_same_build(client):
    status = client.get("/api/status").json()
    assert status["build"] == client.get("/api/version").json()


def test_build_info_prefers_the_environment(monkeypatch):
    """A container knows what it was built from; installed metadata only knows what the file
    said at the time."""
    from csi import version as version_module

    version_module.build_info.cache_clear()
    monkeypatch.setenv("CSI_VERSION", "9.9.9")
    monkeypatch.setenv("CSI_COMMIT", "abc1234def")
    try:
        info = version_module.build_info()
        assert info["version"] == "9.9.9"
        assert version_module.version_string() == "9.9.9 (abc1234)"
    finally:
        version_module.build_info.cache_clear()


def test_unset_build_args_do_not_become_a_version(monkeypatch):
    """`--build-arg` leaves the literal placeholder behind when a caller passes nothing."""
    from csi import version as version_module

    version_module.build_info.cache_clear()
    monkeypatch.setenv("CSI_VERSION", "unknown")
    monkeypatch.setenv("CSI_COMMIT", "")
    try:
        info = version_module.build_info()
        assert info["version"] != "unknown"
        assert info["commit"] == ""
    finally:
        version_module.build_info.cache_clear()
