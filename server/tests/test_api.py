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


# -- access points -----------------------------------------------------------------------


PUBLISHED_APS = {
    "updated_at": 1785600000,
    "follow": "manual",
    "uplink": "wired",
    "measured": {"bssid": "7a:da:88:a2:e1:9f", "chanspec": "9/20"},
    "probe": {
        "host": "192.168.0.1",
        "hz": 200,
        "mode": "unicast",
        "induced_hz": 78.0,
        "total_hz": 102.0,
        "background_hz": 24.0,
    },
    "aps": [
        {"bssid": "7a:da:88:a2:e1:9f", "ssid": "mesh", "channel": 9, "signal": 45,
         "in_use": False},
        {"bssid": "aa:bb:cc:dd:ee:ff", "ssid": "mesh", "channel": 1, "signal": 90,
         "in_use": True},
    ],
}


def test_aps_without_a_host_agent(client):
    """A node whose agent has not published yet is a normal state, not a failure: the picker
    has to be able to say so rather than the API breaking."""
    body = client.get("/api/aps").json()
    assert body == {"available": False, "aps": []}


def test_aps_survives_a_half_written_file(client, tmp_path):
    (tmp_path / "aps.json").write_text('{"updated_at": 178560')
    assert client.get("/api/aps").json()["available"] is False


def test_aps_reports_what_the_host_published(client, tmp_path):
    (tmp_path / "aps.json").write_text(json.dumps(PUBLISHED_APS))
    body = client.get("/api/aps").json()

    assert body["available"] is True
    assert body["measured"]["chanspec"] == "9/20"
    assert [ap["bssid"] for ap in body["aps"]] == [ap["bssid"] for ap in PUBLISHED_APS["aps"]]


def test_aps_relays_the_uplink_and_the_measured_yield(client, tmp_path):
    """How traffic is being provoked, and what that is buying, are the agent's to report and the
    server's only to carry: it cannot see the interfaces or count the frames itself."""
    (tmp_path / "aps.json").write_text(json.dumps(PUBLISHED_APS))
    body = client.get("/api/aps").json()

    assert body["uplink"] == "wired"
    assert body["probe"]["mode"] == "unicast"
    assert body["probe"]["induced_hz"] == 78.0
    assert body["probe"]["background_hz"] == 24.0


def test_aps_from_an_older_agent_keeps_working(client, tmp_path):
    """An agent that predates the probe-mode fields publishes neither them nor an uplink. That
    has to relay as an absence — the UI says "not reported" — rather than as a failure or an
    invented default."""
    published = {**PUBLISHED_APS, "probe": {"host": "192.168.0.1", "hz": 200}}
    del published["uplink"]
    (tmp_path / "aps.json").write_text(json.dumps(published))
    body = client.get("/api/aps").json()

    assert body["available"] is True
    assert "uplink" not in body
    assert "mode" not in body["probe"]
    assert "induced_hz" not in body["probe"]


def test_select_writes_a_request_the_agent_can_read(client, tmp_path):
    posted = client.post(
        "/api/aps/select",
        json={"mode": "manual", "bssid": "AA:BB:CC:DD:EE:FF", "probe_host": "192.168.0.1"},
    ).json()

    request = json.loads((tmp_path / "ap-select.request.json").read_text())
    assert request["id"] == posted["id"]
    assert request["mode"] == "manual"
    assert request["bssid"] == "aa:bb:cc:dd:ee:ff"
    assert request["probe_host"] == "192.168.0.1"


def test_select_auto_needs_no_bssid(client, tmp_path):
    assert client.post("/api/aps/select", json={"mode": "auto"}).status_code == 200
    assert json.loads((tmp_path / "ap-select.request.json").read_text())["mode"] == "auto"


def test_select_carries_the_probe_mode_and_rate(client, tmp_path):
    client.post(
        "/api/aps/select",
        json={"mode": "auto", "probe_mode": "Unicast", "probe_hz": 250},
    )
    request = json.loads((tmp_path / "ap-select.request.json").read_text())

    assert request["probe_mode"] == "unicast"
    assert request["probe_hz"] == 250.0


def test_select_leaves_the_probe_alone_when_it_is_not_asked_about(client, tmp_path):
    """Moving the measurement must not silently reset how it is being provoked, so an omitted
    field has to stay distinguishable from a chosen one all the way to the host agent."""
    client.post("/api/aps/select", json={"mode": "auto"})
    request = json.loads((tmp_path / "ap-select.request.json").read_text())

    assert request["probe_mode"] == ""
    assert request["probe_hz"] is None


def test_select_accepts_a_zero_probe_rate(client, tmp_path):
    """0 Hz is a real setting — measure only what the network already carries — and must not be
    confused with "no opinion"."""
    client.post("/api/aps/select", json={"mode": "auto", "probe_hz": 0})
    assert json.loads((tmp_path / "ap-select.request.json").read_text())["probe_hz"] == 0.0


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "sideways", "bssid": "aa:bb:cc:dd:ee:ff"},
        {"mode": "manual"},
        {"mode": "manual", "bssid": "not-a-mac"},
        {"mode": "manual", "bssid": "aa:bb:cc:dd:ee:ff", "probe_host": "--auto"},
        {"mode": "auto", "probe_mode": "shout"},
        {"mode": "auto", "probe_hz": -1},
        {"mode": "auto", "probe_hz": 100000},
        {"mode": "auto", "probe_hz": "fast"},
        # JSON has no NaN or infinity literal, so the way one reaches the host is as a string
        # that float() happily accepts. Both fail the range check rather than the parse.
        {"mode": "auto", "probe_hz": "nan"},
        {"mode": "auto", "probe_hz": "inf"},
        # float(True) is 1.0, so a JSON true would otherwise arrive as a 1 Hz probe.
        {"mode": "auto", "probe_hz": True},
    ],
)
def test_select_rejects_bad_input(client, tmp_path, body):
    assert client.post("/api/aps/select", json=body).status_code == 400
    assert not (tmp_path / "ap-select.request.json").exists()


def test_select_result_is_pending_until_the_agent_answers(client, tmp_path):
    request_id = client.post("/api/aps/select", json={"mode": "auto"}).json()["id"]
    assert client.get(f"/api/aps/select/{request_id}").json() == {"pending": True}

    # A leftover from an earlier selection must not be read as an answer to this one: the host
    # keeps a single result slot, so the id is the only thing that ties one to the other.
    result = tmp_path / "ap-select.result.json"
    result.write_text(json.dumps({"id": "an-older-one", "ok": True, "finished_at": 1785600000}))
    assert client.get(f"/api/aps/select/{request_id}").json() == {"pending": True}

    result.write_text(
        json.dumps({"id": request_id, "ok": False, "error": "no such AP",
                    "finished_at": 1785600001})
    )
    answered = client.get(f"/api/aps/select/{request_id}").json()
    assert answered["ok"] is False
    assert answered["error"] == "no such AP"


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
