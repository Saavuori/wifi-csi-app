"""HTTP and WebSocket surface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .echo import start_echo
from .hub import Client, Hub
from .ingest import start_listener
from .recorder import scan_recording
from .version import build_info

log = logging.getLogger("csi.api")


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

    @app.get("/api/version")
    async def version() -> dict:
        return build_info()

    # -- config ---------------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config() -> dict:
        return hub.config_dict()

    @app.patch("/api/config")
    async def patch_config(patch: dict = Body(...)) -> dict:
        return hub.update_config(patch)

    @app.post("/api/recalibrate")
    async def recalibrate(body: dict = Body(default={})) -> dict:
        try:
            node_id = _node_id(body.get("node_id"))
        except ValueError as exc:
            raise HTTPException(400, "node_id must be an integer") from exc
        hub.recalibrate(node_id)
        return {"ok": True}

    # -- node control and the WiFi overview -----------------------------------------------

    @app.get("/api/wifi")
    async def wifi() -> dict:
        return hub.wifi_report()

    @app.get("/api/nodes/{node_id}/control")
    async def get_node_control(node_id: int) -> dict:
        _check_node_id(node_id)
        return hub.control.get(node_id)

    @app.patch("/api/nodes/{node_id}/control")
    async def patch_node_control(node_id: int, body: dict = Body(...)) -> dict:
        _check_node_id(node_id)
        return hub.patch_node_control(node_id, body)

    @app.post("/api/nodes/{node_id}/control/scan")
    async def request_node_scan(node_id: int) -> dict:
        _check_node_id(node_id)
        return hub.request_node_scan(node_id)

    @app.post("/api/nodes/{node_id}/control/report")
    async def report_node_control(node_id: int, body: dict = Body(...)) -> dict:
        """The node's half of the loop: what it applied, and scan results when it has fresh
        ones. Kept separate from PATCH so a node can never alter what the operator asked for."""
        _check_node_id(node_id)
        return hub.report_node_control(node_id, body)

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


def _check_node_id(node_id: int) -> None:
    """Path-parameter validation shared by the control endpoints. 0 and 255 are reserved on
    the wire, and an id outside a byte could never appear in a frame — storing desired config
    for it would create a phantom node the UI then dutifully renders."""
    if not 1 <= node_id <= 254:
        raise HTTPException(400, "node_id must be 1..254")


def _node_id(value: Any) -> int | None:
    """A node id from a client, or None for "all nodes". Raises ValueError on anything else.

    `bool` is excluded deliberately: it is an `int` in Python, so `{"node_id": true}` would
    otherwise recalibrate node 1 and nothing would say so.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(value)
    return int(value)


def _handle_client_message(hub: Hub, client: Client, raw: str) -> None:
    """Apply one message from a browser. Never raises.

    The caller is the socket's own receive loop, which catches only `WebSocketDisconnect` and
    `RuntimeError` — so anything else escaping here does not fail the message, it fails the
    connection, and the browser reconnects having lost every view's state. A malformed message
    is worth ignoring, never worth a disconnect.
    """
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return

    try:
        _apply_client_message(hub, client, message)
    except (TypeError, ValueError) as exc:
        log.debug("ignoring malformed client message %r: %s", message.get("type"), exc)


def _apply_client_message(hub: Hub, client: Client, message: dict) -> None:
    kind = message.get("type")
    if kind == "subscribe":
        nodes = message.get("nodes")
        client.nodes = {int(n) for n in nodes} if isinstance(nodes, list) else None
        if "frames" in message:
            client.frames = bool(message["frames"])
        if "decimate" in message:
            client.decimate = max(1, int(message["decimate"]))
    elif kind == "config":
        config = message.get("config")
        hub.update_config(config if isinstance(config, dict) else {})
    elif kind == "recalibrate":
        hub.recalibrate(_node_id(message.get("node_id")))
    elif kind == "ping":
        client.send({"type": "pong", "t": message.get("t")})


def _suggested_labels() -> tuple[str, ...]:
    from .sessions import SUGGESTED_LABELS

    return SUGGESTED_LABELS
