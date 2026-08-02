"""HTTP and WebSocket surface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .echo import start_echo
from .hub import Client, Hub
from .ingest import start_listener
from .recorder import scan_recording

log = logging.getLogger("csi.api")

# The three files the access-point picker speaks through. They live in the data directory
# because that is the one place the server and the host can both reach: the server runs in a
# container and nmcli, systemctl and the nexmon tools do not.
APS_FILE = "aps.json"
SELECT_REQUEST_FILE = "ap-select.request.json"
SELECT_RESULT_FILE = "ap-select.result.json"

_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")

# A probe host is handed to a tool running as root on the host, so keep it to something that
# can only be a host. The leading character must be alphanumeric in particular: a value opening
# with a dash would arrive as an option rather than as an argument.
_PROBE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}")

# How the node provokes traffic out of the radio it is measuring. The list is the node's, not
# ours (csi_node.py's Prober.MODES), and the check exists so that a typo becomes a 400 here
# rather than an argument the host tool does not recognise.
_PROBE_MODES = ("broadcast", "unicast", "icmp")

# A guard rail, not a tuned limit. Capture is capped at CSI_MAX_HZ (100) and the measured yield
# tops out well below the send rate — 300 Hz of broadcast bought 166 Hz — so nothing above a few
# hundred buys anything. The ceiling is here so that a slipped decimal point cannot turn the node
# into a flooder on a network it shares with everything else in the house.
_PROBE_HZ_MAX = 1000.0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    hub = Hub(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        transport = await start_listener(hub, settings.udp_host, settings.udp_port)
        echo = None
        if settings.echo_port:
            echo = await start_echo(settings.udp_host, settings.echo_port)
        try:
            yield
        finally:
            if echo is not None:
                echo.close()
            transport.close()
            await hub.stop()

    app = FastAPI(
        title="WiFi CSI",
        version="1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.settings = settings

    # -- status ---------------------------------------------------------------------------

    @app.get("/api/status")
    async def status() -> dict:
        return hub.snapshot()

    @app.get("/api/nodes")
    async def nodes() -> dict:
        return {"nodes": hub.node_report()}

    @app.get("/api/healthz")
    async def healthz() -> dict:
        return {"ok": True, "uptime_s": hub.snapshot()["uptime_s"]}

    # -- config ---------------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config() -> dict:
        return hub.config_dict()

    @app.patch("/api/config")
    async def patch_config(patch: dict = Body(...)) -> dict:
        return hub.update_config(patch)

    @app.post("/api/recalibrate")
    async def recalibrate(body: dict = Body(default={})) -> dict:
        node_id = body.get("node_id")
        hub.recalibrate(int(node_id) if node_id is not None else None)
        return {"ok": True}

    # -- access points --------------------------------------------------------------------
    #
    # Choosing which radio to measure cannot strand the machine. The capture radio is not the
    # radio the node is reached over: wlan0 monitors whatever channel it is pointed at while the
    # USB dongle stays associated wherever it likes, and a selection never touches that
    # association. So the worst a bad choice here can do is measure a dull path.
    #
    # The work itself happens on the host, which watches the data directory for a request and
    # writes back a result. That agent may not be running at all, so every read below treats a
    # missing or unreadable file as a state to report rather than an error.

    @app.get("/api/aps")
    async def access_points() -> dict:
        """The radios the host last saw, and which one is currently being measured.

        `available` is false when the host agent has published nothing yet, which is ordinary
        on a node that has just booted; the UI needs to tell that apart from an empty scan.

        Everything else is relayed exactly as the agent wrote it, and deliberately so: the agent
        knows things the server cannot see (which interface is the uplink, what the probes are
        actually inducing) and it gets to grow new fields without a server release. The only
        contract is `aps`. An older agent that publishes no uplink or no probe mode simply leaves
        those keys out, and the UI reports them as unknown rather than inventing a value.
        """
        published = _read_json(settings.data_dir / APS_FILE)
        if not isinstance(published, dict) or not isinstance(published.get("aps"), list):
            return {"available": False, "aps": []}
        return {**published, "available": True}

    @app.post("/api/aps/select")
    async def select_access_point(body: dict = Body(...)) -> dict:
        """Ask the host to measure a different radio.

        Returns immediately with an id: applying a selection restarts the capture on the host
        and takes seconds, so the caller polls the result rather than holding a request open.
        """
        # "rescan" asks the host to go and look again rather than to change anything. It only
        # means something on a wired node, where the list comes from sweeping the monitor radio
        # across channels and is therefore as old as the last sweep; with a dongle the agent
        # rebuilds it from nmcli on every tick and the host answers immediately.
        mode = str(body.get("mode") or "").strip().lower()
        if mode not in ("manual", "auto", "rescan"):
            raise HTTPException(400, f"unknown mode {mode!r}")

        bssid = str(body.get("bssid") or "").strip().lower()
        if mode == "manual" and not _MAC.fullmatch(bssid):
            raise HTTPException(400, f"not a BSSID: {bssid!r}")

        # The probe host must be a host the measured radio transmits to — in unicast mode one of
        # its wireless clients, in icmp mode something whose replies come back through it.
        # Nothing here can check that; it is the operator's to get right, and only the syntax is
        # ours.
        probe_host = str(body.get("probe_host") or "").strip()
        if probe_host and not _PROBE_HOST.fullmatch(probe_host):
            raise HTTPException(400, f"not a host: {probe_host!r}")

        probe_mode = str(body.get("probe_mode") or "").strip().lower()
        if probe_mode and probe_mode not in _PROBE_MODES:
            raise HTTPException(400, f"unknown probe mode {probe_mode!r}")

        probe_hz = _probe_hz(body.get("probe_hz"))

        # An omitted probe field means "leave the host's setting alone" rather than "turn it
        # off": the picker sends only what the operator actually chose, and a client that only
        # wants to move the measurement must not silently reset how it is being provoked. Empty
        # string and null are the two ways of saying nothing, one per field type.
        request_id = uuid.uuid4().hex
        _write_json(
            settings.data_dir / SELECT_REQUEST_FILE,
            {
                "id": request_id,
                "mode": mode,
                "bssid": bssid if mode == "manual" else "",
                "probe_host": probe_host,
                "probe_mode": probe_mode,
                "probe_hz": probe_hz,
            },
        )
        return {"id": request_id}

    @app.get("/api/aps/select/{request_id}")
    async def select_access_point_result(request_id: str) -> dict:
        """Whether a selection has finished, and whether it worked.

        The host keeps one result slot rather than a file per request, so a result answers this
        call only when the ids match. A leftover from an earlier selection reads as pending,
        which is the honest answer: ours has not landed yet.
        """
        result = _read_json(settings.data_dir / SELECT_RESULT_FILE)
        if isinstance(result, dict) and result.get("id") == request_id:
            return result
        return {"pending": True}

    # -- sessions -------------------------------------------------------------------------

    @app.get("/api/sessions")
    async def list_sessions() -> dict:
        return {
            "sessions": [s.as_dict() for s in hub.sessions.sorted()],
            "labels": list(_suggested_labels()),
        }

    @app.post("/api/sessions")
    async def start_recording(body: dict = Body(default={})) -> dict:
        label = str(body.get("label") or "unlabelled")
        notes = str(body.get("notes") or "")
        return {"session": hub.start_recording(label, notes)}

    @app.post("/api/recording/stop")
    async def stop_recording() -> dict:
        return {"session": hub.stop_recording()}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict:
        if hub.recorder is not None and hub.recorder.session.id == session_id:
            raise HTTPException(409, "cannot delete the session currently being recorded")
        if not hub.sessions.delete(session_id):
            raise HTTPException(404, "no such session")
        return {"ok": True}

    @app.patch("/api/sessions/{session_id}")
    async def edit_session(session_id: str, body: dict = Body(...)) -> dict:
        session = hub.sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "no such session")
        if "label" in body:
            session.label = str(body["label"])
        if "notes" in body:
            session.notes = str(body["notes"])
        hub.sessions.update(session)
        return {"session": session.as_dict()}

    @app.post("/api/sessions/{session_id}/rescan")
    async def rescan_session(session_id: str) -> dict:
        """Rebuild frame counts and time bounds by walking the file.

        Needed after an unclean shutdown, where the on-disk recording is complete but the
        metadata was last flushed some seconds earlier.
        """
        session = hub.sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "no such session")
        path = hub.sessions.file_for(session)
        if not path.exists():
            raise HTTPException(404, "recording file is missing")

        summary = await asyncio.to_thread(scan_recording, path)
        session.frames = summary["frames"]
        session.node_ids = summary["node_ids"]
        session.first_t_us = summary["first_t_us"]
        session.last_t_us = summary["last_t_us"]
        session.n_sub = summary["n_sub"]
        session.bytes = summary["bytes"]
        hub.sessions.update(session)
        return {"session": session.as_dict(), "bad_records": summary["bad"]}

    # -- replay ---------------------------------------------------------------------------

    @app.post("/api/sessions/{session_id}/replay")
    async def start_replay(session_id: str, body: dict = Body(default={})) -> dict:
        try:
            state = await hub.start_replay(
                session_id,
                speed=float(body.get("speed", 1.0)),
                loop=bool(body.get("loop", False)),
                start_us=int(body["start_us"]) if body.get("start_us") is not None else None,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, "recording file is missing") from exc
        return {"replay": state}

    @app.post("/api/replay/stop")
    async def stop_replay() -> dict:
        await hub.stop_replay()
        return {"replay": None}

    @app.post("/api/replay/control")
    async def control_replay(body: dict = Body(...)) -> dict:
        replayer = hub.replayer
        if replayer is None:
            raise HTTPException(409, "no replay is running")
        action = body.get("action")
        if action == "pause":
            replayer.pause()
        elif action == "resume":
            replayer.resume()
        elif action == "speed":
            replayer.set_speed(float(body.get("speed", 1.0)))
        elif action == "seek":
            hub.seek_replay(int(body.get("t_us", 0)))
        else:
            raise HTTPException(400, f"unknown action {action!r}")
        return {"replay": replayer.state()}

    # -- websocket ------------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        client = Client()
        hub.add_client(client)
        writer = asyncio.create_task(_pump(ws, client))
        try:
            while True:
                message = await ws.receive_text()
                _handle_client_message(hub, client, message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.remove_client(client)
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    # -- static ---------------------------------------------------------------------------

    web_dir = settings.web_dir
    if web_dir and web_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str):
            candidate = (web_dir / path).resolve() if path else None
            if candidate and candidate.is_file() and web_dir in candidate.parents:
                return FileResponse(candidate)
            index = web_dir / "index.html"
            if index.is_file():
                return FileResponse(index)
            return JSONResponse({"detail": "web app not built"}, status_code=404)
    else:
        log.warning("no web directory at %s; serving the API only", web_dir)

    return app


async def _pump(ws: WebSocket, client: Client) -> None:
    """Drain one client's queue onto its socket.

    Bytes go out as binary frames, dicts as JSON text. The client never blocks the hub: if this
    task falls behind, `Client.send` drops the oldest frames rather than applying backpressure
    to ingest.
    """
    while True:
        message: Any = await client.queue.get()
        if isinstance(message, (bytes, bytearray, memoryview)):
            await ws.send_bytes(message)
        else:
            await ws.send_text(json.dumps(message, separators=(",", ":")))


def _handle_client_message(hub: Hub, client: Client, raw: str) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return

    kind = message.get("type")
    if kind == "subscribe":
        nodes = message.get("nodes")
        client.nodes = set(int(n) for n in nodes) if isinstance(nodes, list) else None
        if "frames" in message:
            client.frames = bool(message["frames"])
        if "decimate" in message:
            client.decimate = max(1, int(message["decimate"]))
    elif kind == "config":
        hub.update_config(message.get("config", {}))
    elif kind == "recalibrate":
        node_id = message.get("node_id")
        hub.recalibrate(int(node_id) if node_id is not None else None)
    elif kind == "ping":
        client.send({"type": "pong", "t": message.get("t")})


def _read_json(path: Path) -> Any:
    """Read a file another process owns, reporting any problem as "nothing to read".

    The host agent renames its files into place, so a reader normally sees a whole document or
    no file at all. Everything else — the agent not running yet, a rename that was not atomic,
    a truncated write — is a state the callers already have an answer for, and none of it is
    worth an exception. A half-written file may not even decode as UTF-8, hence ValueError
    rather than only JSONDecodeError.
    """
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload: dict) -> None:
    """Publish a file the host agent is polling for, so it cannot pick up half a request.

    Same shape as the session index: a temporary file in the same directory, then a rename.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fp:
            json.dump(payload, fp, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _probe_hz(value: Any) -> float | None:
    """Validate a requested probe rate, returning None when the caller did not ask for one.

    Zero is a legitimate request — measure only what the network already carries — so "absent"
    has to mean something different from "zero", and only absent leaves the host's rate alone.
    Bools are rejected before float() sees them, because `float(True)` is 1.0 and a JSON `true`
    would otherwise arrive on the host as a 1 Hz probe.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise HTTPException(400, f"not a probe rate: {value!r}")
    try:
        hz = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"not a probe rate: {value!r}") from exc
    # NaN fails this as well, since every comparison against it is false.
    if not 0.0 <= hz <= _PROBE_HZ_MAX:
        raise HTTPException(400, f"probe rate outside 0-{_PROBE_HZ_MAX:g} Hz: {hz!r}")
    return hz


def _suggested_labels() -> tuple[str, ...]:
    from .sessions import SUGGESTED_LABELS

    return SUGGESTED_LABELS
