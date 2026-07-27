"""UDP listener.

UDP rather than a WebSocket or TCP because the device should not have to own reconnect logic,
and because a lost CSI frame is survivable — it becomes a sequence gap, which is measured, and
the resampler in the DSP layer already assumes irregular arrival. A stalled TCP send inside a
firmware task is much more damaging than a missing frame.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .hub import Hub

log = logging.getLogger("csi.ingest")

# Enough for a 256-subcarrier Nexmon frame with room to spare. Anything larger is not ours.
MAX_DATAGRAM = 4096

# Ask the kernel for a large receive buffer. The default on Linux is around 200 KB, which is
# only about a second of two nodes at 80 Hz — a GC pause or a slow disk flush would start
# dropping frames in the socket, where nothing can count them.
RCVBUF_BYTES = 4 << 20


class CSIProtocol(asyncio.DatagramProtocol):
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("socket")
        if sock is not None:
            import socket as _socket

            try:
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVBUF, RCVBUF_BYTES)
            except OSError as exc:
                log.warning("could not enlarge the UDP receive buffer: %s", exc)

    def datagram_received(self, data: bytes, addr) -> None:
        self.hub.handle_datagram(data, time.time())

    def error_received(self, exc: Exception) -> None:
        # ICMP port-unreachable from a previous send, usually. Not fatal for a receiver.
        log.debug("udp error: %s", exc)


async def start_listener(hub: Hub, host: str, port: int) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: CSIProtocol(hub), local_addr=(host, port)
    )
    log.info("listening for CSI on udp://%s:%d", host, port)
    return transport  # type: ignore[return-value]
