#!/usr/bin/env python3
"""Raspberry Pi CSI node: nexmon_csi capture -> the uplink wire format.

The Pi is a third kind of node alongside the two ESP32 roles. nexmon_csi patches the BCM43455
firmware to hand CSI to the host as UDP broadcasts on port 5500; this turns those into the
`docs/wire-format.md` uplink datagrams the server already ingests, so nothing downstream knows
it is looking at a Pi.

The node is a station probing its access point, which is the same topology the ESP32 firmware
uses — and for the same reason. An associated radio is only handed CSI for frames addressed to
it, so a node that only listens samples whenever the network happens to talk to it. Generating
the traffic is what makes the rate a property of the node.

Some of what the wire format carries is not in a nexmon packet, and each gap is handled here:

  seq        nexmon reports the *802.11* sequence number of the frame that triggered the
             capture. That is 12 bits, wraps every 4096 frames, and belongs to the
             transmitter. Fed to the server it would wrap every ~41 s at 100 Hz and read as a
             reboot. We mint our own monotonic counter instead.
  timestamp  no radio-level timestamp exists. We use the kernel's receive timestamp
             (SO_TIMESTAMPNS), which is stamped in the driver rather than in this process, so
             it does not carry our scheduling jitter. This matters: the breathing estimator
             resamples on these timestamps and cannot repair one that is wrong.
  rssi       upstream nexmon_csi carries no RSSI. Because the Pi stays associated we can read
             the driver's own estimate from /proc/net/wireless. It is smoothed and slow, which
             happens to be exactly what the server wants — its hybrid normalization only uses
             RSSI through a 30-second EMA (see server/csi/dsp/preprocess.py).
  channel    decoded from the chanspec in the packet.
  link_epoch nexmon has no notion of one, but it reports the transmitter and the chanspec of
             every frame, and a change in either is exactly what the epoch exists to signal.

src_mac is the one v2 field that arrives for free: it is in every nexmon packet.

This file is a fourth mirror of the uplink header, alongside firmware/main/csi_wire.h,
server/csi/protocol.py and web/src/lib/protocol.ts. pi/tests/test_csi_node.py round-trips
every datagram through the server's own parser so the two cannot drift.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import struct
import sys
import threading
import time

import numpy as np

log = logging.getLogger("csi.pi")

# -- the uplink wire format (mirror of server/csi/protocol.py) ---------------------------

WIRE_MAGIC = 0x4353
WIRE_VERSION = 2
# v2: v1's 22 bytes, then src_mac, link_epoch and one reserved byte. Both of the appended
# fields are ones a nexmon packet can actually answer — src_mac is in every packet, and the
# epoch is what a change of transmitter or chanspec means.
_WIRE_HEADER = struct.Struct("<HBBIQbbBBH6sBB")
assert _WIRE_HEADER.size == 30

SEC_CHANNEL_NONE = 0
SEC_CHANNEL_ABOVE = 1
SEC_CHANNEL_BELOW = 2

# -- nexmon_csi's own packet ------------------------------------------------------------

# Since seemoo-lab/nexmon_csi#256 the payload opens with two magic bytes rather than four.
# Both bytes are 0x11, so this reads the same either endianness.
NEXMON_MAGIC = 0x1111
# magic u16 | src_mac 6 | seq u16 | core/spatial u16 | chanspec u16 | chip u16
NEXMON_HEADER_SIZE = 16

ETH_P_IP = 0x0800
IPPROTO_UDP = 17
NEXMON_UDP_PORT = 5500

# Linux-only, and both are 35. Named here with a fallback so this module imports on a
# developer machine — the tests run anywhere, the node itself only ever runs on a Pi.
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
SCM_TIMESTAMPNS = getattr(socket, "SCM_TIMESTAMPNS", 35)

# Broadcom chanspec, d11ac encoding.
CHANSPEC_CHAN_MASK = 0x00FF
CHANSPEC_BW_MASK = 0x3800
CHANSPEC_BW_20 = 0x1000
CHANSPEC_SB_MASK = 0x0700
CHANSPEC_SB_LOWER = 0x0000


class NexmonPacket:
    """One decoded nexmon CSI packet. `csi` is complex64, length n_sub."""

    __slots__ = ("src_mac", "chanspec", "core", "spatial", "csi")

    def __init__(self, src_mac: bytes, chanspec: int, core: int, spatial: int, csi: np.ndarray):
        self.src_mac = src_mac
        self.chanspec = chanspec
        self.core = core
        self.spatial = spatial
        self.csi = csi

    @property
    def n_sub(self) -> int:
        return int(self.csi.size)


def parse_nexmon(payload: bytes) -> NexmonPacket | None:
    """Decode one nexmon CSI payload, or None if it is not one.

    Returns None rather than raising: this runs on whatever the capture socket hands over, and
    a malformed packet is a thing to count, not a thing to crash on.
    """
    if len(payload) < NEXMON_HEADER_SIZE:
        return None
    if struct.unpack_from("<H", payload, 0)[0] != NEXMON_MAGIC:
        return None

    body = len(payload) - NEXMON_HEADER_SIZE
    # bcm43455c0 emits an int16 real and an int16 imaginary part per subcarrier.
    if body <= 0 or body % 4 != 0:
        return None
    n_sub = body // 4

    src_mac = payload[2:8]
    core_spatial = struct.unpack_from("<H", payload, 10)[0]
    chanspec = _plausible_chanspec(payload, 12)

    raw = np.frombuffer(payload, dtype="<i2", count=2 * n_sub, offset=NEXMON_HEADER_SIZE)
    # nexmon interleaves (real, imag). The uplink wire is (imag, real) because that is the
    # order esp_wifi uses; the swap happens once, here.
    csi = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)

    return NexmonPacket(
        src_mac=src_mac,
        chanspec=chanspec,
        core=core_spatial & 0x7,
        spatial=(core_spatial >> 3) & 0x7,
        csi=csi,
    )


def _plausible_chanspec(payload: bytes, offset: int) -> int:
    """Read the chanspec, preferring whichever endianness yields a real channel number.

    Firmware builds have differed on this and getting it wrong mislabels every frame's channel
    while leaving the CSI itself perfectly valid — a failure that is invisible in the
    waterfall. Rather than guess, decode both and let the channel plan decide.
    """
    little = struct.unpack_from("<H", payload, offset)[0]
    big = struct.unpack_from(">H", payload, offset)[0]
    if _is_real_channel(little & CHANSPEC_CHAN_MASK):
        return little
    if _is_real_channel(big & CHANSPEC_CHAN_MASK):
        return big
    return little


def _is_real_channel(ch: int) -> bool:
    return 1 <= ch <= 14 or 32 <= ch <= 177


def chanspec_channel(chanspec: int) -> int:
    return chanspec & CHANSPEC_CHAN_MASK


def chanspec_sec_channel(chanspec: int) -> int:
    """Map the chanspec sideband onto the wire's sec_channel enum.

    The enum only distinguishes above/below, which is exactly right for HT40 and a
    simplification at 80 MHz, where the control channel is one of four positions. It is a
    display field; the subcarrier layout is keyed on n_sub, not on this.
    """
    if (chanspec & CHANSPEC_BW_MASK) == CHANSPEC_BW_20:
        return SEC_CHANNEL_NONE
    if (chanspec & CHANSPEC_SB_MASK) == CHANSPEC_SB_LOWER:
        return SEC_CHANNEL_ABOVE
    return SEC_CHANNEL_BELOW


# -- quantization -----------------------------------------------------------------------

# Peak magnitude a scaled frame is aimed at. Below 127 so that rounding cannot clip.
QUANT_PEAK = 120.0


def quantize(csi: np.ndarray) -> np.ndarray:
    """complex64[n_sub] -> int8[2*n_sub], interleaved (imag, real).

    Scaled per frame so the largest component lands near full scale. That discards the frame's
    absolute gain, which sounds worse than it is: the server's normalization divides by the
    frame's own RMS in both of its default modes, so absolute scale was going to be discarded
    anyway, and int8 is what the ESP32 delivers — the whole DSP was built and tuned on it.

    What it does cost is the AGC step detector in preprocess.py, which looks for a uniform
    frame-to-frame amplitude jump. Per-frame scaling removes the jump before the server sees
    it, so that detector will not fire on Pi frames. The step is gone rather than flagged.
    """
    peak = float(np.max(np.abs(csi))) if csi.size else 0.0
    scale = QUANT_PEAK / peak if peak > 0.0 else 0.0

    out = np.empty(2 * csi.size, dtype=np.int8)
    out[0::2] = np.rint(csi.imag * scale).astype(np.int8)
    out[1::2] = np.rint(csi.real * scale).astype(np.int8)
    return out


def encode_uplink(
    *,
    node_id: int,
    seq: int,
    timestamp_us: int,
    rssi: int,
    noise_floor: int,
    channel: int,
    sec_channel: int,
    data: np.ndarray,
    src_mac: bytes = b"\x00" * 6,
    link_epoch: int = 0,
) -> bytes:
    """Build one uplink datagram. `data` is int8, length 2*n_sub."""
    n_sub = data.size // 2
    header = _WIRE_HEADER.pack(
        WIRE_MAGIC,
        WIRE_VERSION,
        node_id,
        seq & 0xFFFFFFFF,
        timestamp_us & 0xFFFFFFFFFFFFFFFF,
        _clamp_i8(rssi),
        _clamp_i8(noise_floor),
        channel & 0xFF,
        sec_channel & 0xFF,
        n_sub,
        src_mac[:6].ljust(6, b"\x00"),
        link_epoch & 0xFF,
        0,
    )
    return header + data.tobytes()


def _clamp_i8(v: int) -> int:
    return max(-128, min(127, int(v)))


# -- the driver's own view of the link ----------------------------------------------------

WIRELESS_PROC = "/proc/net/wireless"
# Drivers that have no noise estimate report -256. Treat it as absent rather than passing a
# nonsense floor downstream.
NOISE_ABSENT = -200


class LinkStats:
    """RSSI and noise from /proc/net/wireless, refreshed on a timer.

    Sampled slowly on purpose. The server's hybrid normalization feeds RSSI through a
    30-second EMA before using it, so a per-frame value would buy nothing, and reading /proc
    at the frame rate would cost real CPU on a Pi.
    """

    def __init__(self, iface: str, interval_s: float = 1.0, path: str = WIRELESS_PROC) -> None:
        self.iface = iface
        self.interval_s = interval_s
        self.path = path
        self.rssi = 0
        self.noise = 0
        self._next = 0.0

    def refresh(self, now: float, *, force: bool = False) -> None:
        if not force and now < self._next:
            return
        self._next = now + self.interval_s
        sample = read_wireless(self.path, self.iface)
        if sample is not None:
            self.rssi, self.noise = sample


def read_wireless(path: str, iface: str) -> tuple[int, int] | None:
    """(rssi_dbm, noise_dbm) for `iface`, or None if it is not listed.

    The columns after the interface name are status, link, level, noise. Values carry a
    trailing '.' meaning "updated since last read", which is not part of the number.
    """
    try:
        with open(path, encoding="ascii", errors="replace") as fp:
            lines = fp.read().splitlines()
    except OSError:
        return None

    want = iface + ":"
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(want):
            continue
        fields = stripped[len(want) :].split()
        if len(fields) < 4:
            return None
        try:
            level = int(float(fields[2].rstrip(".")))
            noise = int(float(fields[3].rstrip(".")))
        except ValueError:
            return None
        if noise <= NOISE_ABSENT:
            noise = 0
        return level, noise
    return None


# -- capture ------------------------------------------------------------------------------


def open_capture(iface: str) -> socket.socket:
    """A packet socket on `iface` delivering IPv4 frames with kernel receive timestamps.

    AF_PACKET rather than a UDP socket bound to 5500, for two reasons. nexmon sources these
    from 10.10.10.10, an address that is on no local subnet, so reverse-path filtering can
    drop them before they reach a UDP socket — which is why the upstream instructions reach
    for tcpdump. And SOCK_DGRAM here strips the link-layer header, so there is one less
    variable between us and the IP header.
    """
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_IP))
    sock.bind((iface, ETH_P_IP))
    sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
    return sock


def udp_payload(packet: bytes, port: int) -> bytes | None:
    """Payload of an IPv4/UDP packet addressed to `port`, or None."""
    if len(packet) < 20:
        return None
    vhl = packet[0]
    if vhl >> 4 != 4:
        return None
    ihl = (vhl & 0x0F) * 4
    if ihl < 20 or len(packet) < ihl + 8:
        return None
    if packet[9] != IPPROTO_UDP:
        return None
    if struct.unpack_from("!H", packet, ihl + 2)[0] != port:
        return None
    return packet[ihl + 8 :]


def timestamp_us(ancdata) -> int | None:
    """Microseconds from an SCM_TIMESTAMPNS control message, or None if absent."""
    for level, ctype, data in ancdata:
        if level != socket.SOL_SOCKET or ctype != SCM_TIMESTAMPNS:
            continue
        if len(data) < 16:
            return None
        sec, nsec = struct.unpack("qq", data[:16])
        return sec * 1_000_000 + nsec // 1000
    return None


# -- traffic ------------------------------------------------------------------------------


def default_gateway() -> str | None:
    """The IPv4 default gateway, from /proc/net/route."""
    try:
        with open("/proc/net/route", encoding="ascii") as fp:
            rows = fp.read().splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        fields = row.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            raw = int(fields[2], 16)
        except ValueError:
            continue
        return socket.inet_ntoa(struct.pack("<I", raw))
    return None


def icmp_echo(ident: int, seq: int) -> bytes:
    body = struct.pack("!BBHHH", 8, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    return body[:2] + struct.pack("!H", checksum(body)) + body[4:]


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


class Prober(threading.Thread):
    """Pings the access point so that there is something to measure.

    While associated the chip only hands up CSI for frames addressed to this Pi (plus
    broadcast), so with no traffic of our own the rate collapses to the beacon rate. This is
    the same problem the station firmware solves with CSI_PROBE_UDP_ECHO, and the same fix:
    generate the traffic, measure the reply.
    """

    def __init__(self, target: str, hz: float) -> None:
        super().__init__(name="prober", daemon=True)
        self.target = target
        self.interval = 1.0 / hz if hz > 0 else 0.0
        self.sent = 0
        self.failures = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        except PermissionError:
            log.error("probing needs root (raw ICMP); run with --probe-hz 0 to disable")
            return
        sock.setblocking(False)

        ident = os.getpid() & 0xFFFF
        seq = 0
        # Absolute schedule rather than sleep(interval), so a slow send does not let the rate
        # drift downward over a long run.
        due = time.monotonic()
        while not self._stop.is_set():
            try:
                sock.sendto(icmp_echo(ident, seq), (self.target, 0))
                self.sent += 1
            except OSError:
                self.failures += 1
            seq += 1

            due += self.interval
            now = time.monotonic()
            if due < now:
                due = now
            self._stop.wait(due - now)
        sock.close()


# -- the node -----------------------------------------------------------------------------


class Node:
    """Turns nexmon packets into uplink datagrams.

    Kept free of sockets so the whole translation can be tested against synthetic packets.
    """

    def __init__(self, node_id: int, *, core: int = 0, spatial: int = 0) -> None:
        self.node_id = node_id
        self.core = core
        self.spatial = spatial

        self.seq = 0
        self.epoch = 0
        self.rssi = 0
        self.noise = 0

        self.frames = 0
        self.skipped_stream = 0
        self.link_changes = 0

        self._link: tuple[bytes, int] | None = None
        self._t0_us: int | None = None

    def on_packet(self, pkt: NexmonPacket, capture_us: int) -> bytes | None:
        """One nexmon packet in, one uplink datagram out (or None if it is not ours)."""
        # One UDP packet arrives per configured core and spatial stream. Taking more than one
        # would emit several frames sharing a moment, which reads as duplicate sequence
        # numbers or as an impossible rate depending on how they are numbered.
        if pkt.core != self.core or pkt.spatial != self.spatial:
            self.skipped_stream += 1
            return None

        link = (pkt.src_mac, pkt.chanspec)
        if self._link is not None and link != self._link:
            # Roam, reconnect or a channel switch by the access point. Every baseline built on
            # the old link describes a room that is no longer the one being measured, so the
            # epoch goes up and `nodes.py` throws the node's history away — the same response
            # it gives a reboot.
            #
            # Only the epoch changes. The clock and the counter stay monotonic, because a node
            # that also restarted those would be reporting a reboot *and* a roam for one event.
            self.link_changes += 1
            self.epoch = (self.epoch + 1) & 0xFF
            log.info(
                "link changed (%s ch%d -> %s ch%d); epoch now %d",
                _fmt_mac(self._link[0]),
                chanspec_channel(self._link[1]),
                _fmt_mac(pkt.src_mac),
                chanspec_channel(pkt.chanspec),
                self.epoch,
            )
        self._link = link

        if self._t0_us is None:
            self._t0_us = capture_us
        # Device time counts from the first frame, mirroring esp_timer_get_time() counting from
        # boot. Never negative: a timestamp that went backwards would be read as a reboot.
        elapsed = max(0, capture_us - self._t0_us)

        datagram = encode_uplink(
            node_id=self.node_id,
            seq=self.seq,
            timestamp_us=elapsed,
            rssi=self.rssi,
            noise_floor=self.noise,
            channel=chanspec_channel(pkt.chanspec),
            sec_channel=chanspec_sec_channel(pkt.chanspec),
            data=quantize(pkt.csi),
            # The one v2 field nexmon answers directly: the transmitter of the frame this
            # measurement came from, which for an associated Pi is the access point.
            src_mac=pkt.src_mac,
            link_epoch=self.epoch,
        )
        self.seq += 1
        self.frames += 1
        return datagram


def _fmt_mac(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


def run(args: argparse.Namespace) -> int:
    node = Node(args.node_id, core=args.core, spatial=args.spatial)
    stats = LinkStats(args.iface, interval_s=args.rssi_interval)
    stats.refresh(time.monotonic(), force=True)
    node.rssi, node.noise = stats.rssi, stats.noise

    try:
        capture = open_capture(args.iface)
    except PermissionError:
        log.error("capturing needs root (AF_PACKET); try sudo")
        return 1
    except OSError as exc:
        log.error("cannot capture on %s: %s", args.iface, exc)
        return 1

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server = (args.server, args.port)

    prober = None
    if args.probe_hz > 0:
        target = args.probe or default_gateway()
        if target is None:
            log.warning("no default gateway found; not probing. Frames will arrive at the "
                        "beacon rate, which is enough for breathing but not for much else.")
        else:
            log.info("probing %s at %g Hz", target, args.probe_hz)
            prober = Prober(target, args.probe_hz)
            prober.start()

    log.info(
        "node %d: %s -> %s:%d, core %d stream %d",
        args.node_id, args.iface, args.server, args.port, args.core, args.spatial,
    )

    bad = 0
    reported = time.monotonic()
    try:
        while True:
            packet, ancdata, _flags, _addr = capture.recvmsg(4096, socket.CMSG_SPACE(64))
            payload = udp_payload(packet, NEXMON_UDP_PORT)
            if payload is None:
                continue

            pkt = parse_nexmon(payload)
            if pkt is None:
                bad += 1
                continue

            now = time.monotonic()
            stats.refresh(now)
            node.rssi, node.noise = stats.rssi, stats.noise

            capture_us = timestamp_us(ancdata)
            if capture_us is None:
                # No kernel timestamp: fall back, but say so, because this silently degrades
                # the breathing estimate rather than breaking anything visibly.
                capture_us = time.clock_gettime_ns(time.CLOCK_REALTIME) // 1000
                if node.frames == 0:
                    log.warning("no SO_TIMESTAMPNS; timestamps will carry scheduling jitter")

            datagram = node.on_packet(pkt, capture_us)
            if datagram is not None:
                out.sendto(datagram, server)

            if args.status_interval > 0 and now - reported >= args.status_interval:
                rate = node.frames / max(now - reported, 1e-9)
                log.info(
                    "%d frames (%.1f Hz), %d subcarriers, rssi %d dBm, %d link changes, "
                    "%d malformed",
                    node.frames, rate, pkt.n_sub, node.rssi, node.link_changes, bad,
                )
                node.frames = 0
                reported = now
    except KeyboardInterrupt:
        pass
    finally:
        if prober is not None:
            prober.stop()
        capture.close()
        out.close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="csi_node",
        description="Forward nexmon_csi captures to a CSI server as uplink frames.",
    )
    p.add_argument("--server", default=os.environ.get("CSI_SERVER_HOST", "127.0.0.1"),
                   help="server address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=int(os.environ.get("CSI_UDP_PORT", "5566")),
                   help="server UDP port (default 5566)")
    p.add_argument("--iface", default=os.environ.get("CSI_IFACE", "wlan0"),
                   help="interface nexmon delivers on (default wlan0)")
    p.add_argument("--node-id", type=int, default=int(os.environ.get("CSI_NODE_ID", "20")),
                   help="node id, 1..254 (default 20)")
    p.add_argument("--core", type=int, default=0, help="chip core to keep (default 0)")
    p.add_argument("--spatial", type=int, default=0,
                   help="spatial stream to keep (default 0)")
    p.add_argument("--probe", default=os.environ.get("CSI_PROBE_HOST") or None,
                   help="host to ping (default: the IPv4 default gateway)")
    p.add_argument("--probe-hz", type=float,
                   default=float(os.environ.get("CSI_PROBE_HZ", "100")),
                   help="probe rate, 0 to disable (default 100)")
    p.add_argument("--rssi-interval", type=float, default=1.0,
                   help="seconds between /proc/net/wireless reads (default 1)")
    p.add_argument("--status-interval", type=float, default=10.0,
                   help="seconds between status lines, 0 to disable (default 10)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if not 1 <= args.node_id <= 254:
        p.error("--node-id must be 1..254; 0 and 255 are reserved")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
