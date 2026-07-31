"""UDP echo responder, for station nodes whose router will not answer pings.

A single-board node has to provoke its own downlink stream: it sends something, the access point
transmits the reply, and that transmission is the CSI sample. Pinging the gateway is the cheapest
way to do that and the default. But ICMP is a service the router chooses to provide, and some
rate-limit it hard enough that the reply rate collapses — at which point the node needs a target
that answers unconditionally.

This is that target. It echoes the payload back, verbatim, to whoever sent it, and does nothing
else. It is deliberately not part of the ingest path: a bug here must not be able to touch the
frames the recorder is writing.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("csi.echo")

# A probe is tens of bytes. Anything larger is not one of ours, and echoing it back would make
# this a free traffic amplifier for anyone who found the port.
MAX_PROBE = 256


class EchoProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.probes = 0
        self.oversize = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) > MAX_PROBE:
            self.oversize += 1
            return
        self.probes += 1
        if self.transport is not None:
            self.transport.sendto(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("echo error: %s", exc)


async def start_echo(host: str, port: int) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(EchoProtocol, local_addr=(host, port))
    log.info("echoing node probes on udp://%s:%d", host, port)
    return transport  # type: ignore[return-value]
