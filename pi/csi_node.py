#!/usr/bin/env python3
"""Raspberry Pi CSI node: nexmon_csi capture -> the uplink wire format.

The Pi is a third kind of node alongside the two ESP32 roles. nexmon_csi patches the BCM43455
firmware to hand CSI to the host as UDP broadcasts on port 5500; this turns those into the
`docs/wire-format.md` uplink datagrams the server already ingests, so nothing downstream knows
it is looking at a Pi.

A node that only listens samples whenever the network happens to talk near it, which makes the
rate a property of the household rather than of the node. Generating the traffic is what fixes
that, and there are two ways to do it here. `Prober` pings the access point over the wireless
interface. `MulticastStimulus` emits multicast on the *wired* interface and lets the access
point put it on the air, which is what a monitoring-only Pi — radio in monitor mode, Ethernet
as its backbone — has available. The second costs subcarriers, for the reason `occupied_span`
describes.

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

# -- the Ethernet stimulus ----------------------------------------------------------------

# Any group inside 224.0.0.0/24 is the local network control block, which switches and access
# points forward on every port regardless of IGMP snooping (RFC 4541 §2.1.2). That range is the
# whole reason this works. The obvious-looking choice — an administratively scoped group like
# 239.1.1.1 — is exactly what a snooping access point prunes, because nothing on the wireless
# side has joined it, and the stimulus would then be emitted flawlessly and never reach the air.
# Anything else in the block is equally fine; anything outside it is a bet on how this
# particular access point is configured.
STIMULUS_GROUP = "224.0.0.200"
STIMULUS_PORT = 5510
# Enough bytes to hold the air for a few hundred microseconds at the basic rate, which is what
# buys a decent channel estimate. The text is here so that a tcpdump on some other machine says
# what this traffic is instead of looking like a stray flood.
STIMULUS_PAYLOAD = b"csi-node stimulus".ljust(200, b"\x00")
# How long the capture loop will block for a frame before waking to run the gate and the status
# line anyway. Short enough that a silent channel is noticed promptly, long enough to cost
# nothing on a Pi.
CAPTURE_WAKEUP_S = 0.5

# Linux-only, and both are 35. Named here with a fallback so this module imports on a
# developer machine — the tests run anywhere, the node itself only ever runs on a Pi.
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
SCM_TIMESTAMPNS = getattr(socket, "SCM_TIMESTAMPNS", 35)
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)

# `struct timespec` as SCM_TIMESTAMPNS delivers it, keyed by the length of the control message:
# two 64-bit words on a 64-bit userspace, two 32-bit words on a 32-bit one. See `timestamp_us`.
# "=" is native byte order — the message comes from this machine's own kernel — with explicit
# widths, so the decode follows the message rather than however this interpreter was built.
_TIMESPEC_FORMATS = {16: "=qq", 8: "=ii"}

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


# -- narrowband frames --------------------------------------------------------------------

# nexmon reports the chanspec the *chip* is tuned to, not the width of the frame that triggered
# the capture, and hands up a full FFT either way. So a 20 MHz PPDU arriving while the chip sits
# on an 80 MHz chanspec comes out as 256 subcarriers of which 64 carry signal and 192 carry
# noise — indistinguishable by shape from a real 80 MHz measurement.
#
# This is not a corner case, and it is not new with the stimulus. Every beacon is legacy 20 MHz,
# and so is everything an access point sends to a broadcast address, which is precisely what
# MulticastStimulus provokes. Forwarded whole, each of those frames asks the server to normalize
# across three quarters of noise.
#
# Detection is a comparison between 64-subcarrier blocks: a narrowband frame puts all of its
# energy into one of them.
NARROWBAND_BLOCK = 64
# A real full-width frame can be deeply frequency-selective, but not by this much once each
# block is averaged down to one number. Too high leaves noise in the frame; too low truncates a
# good one. Both are quiet failures, which is why it is a named constant and a flag.
NARROWBAND_MARGIN_DB = 12.0


def occupied_span(csi: np.ndarray, margin_db: float = NARROWBAND_MARGIN_DB) -> slice | None:
    """The 20 MHz block a narrowband frame occupies, or None if the frame fills the capture.

    None for a capture that is already 20 MHz: there is nothing to compare against and nothing
    to trim.

    The block index deliberately does not become a channel number. Which quarter of the FFT is
    which 20 MHz sub-band depends on whether nexmon hands its output in frequency order or in
    raw FFT order, and that is a convention this has not confirmed on hardware. The trim itself
    does not care, because the sub-bands are contiguous 64-bin blocks under either convention —
    but a channel derived from the index would be a coin flip, and a confidently wrong channel
    is the exact failure `_plausible_chanspec` exists to avoid.
    """
    blocks = csi.size // NARROWBAND_BLOCK
    if blocks < 2 or csi.size % NARROWBAND_BLOCK:
        return None

    power = (np.abs(csi) ** 2).reshape(blocks, NARROWBAND_BLOCK).mean(axis=1)
    best = int(np.argmax(power))
    if power[best] <= 0.0:
        return None

    # Against the loudest of the others rather than their average: three quiet blocks and one
    # loud one is the shape being looked for, and taking the maximum is the reading least likely
    # to call a real frame narrow.
    loudest_other = float(np.delete(power, best).max())
    if loudest_other > 0.0 and 10.0 * np.log10(power[best] / loudest_other) < margin_db:
        return None
    return slice(best * NARROWBAND_BLOCK, (best + 1) * NARROWBAND_BLOCK)


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
    """Microseconds from an SCM_TIMESTAMPNS control message, or None if absent.

    The message carries a `struct timespec`, and its width follows userspace, not the kernel:
    two 64-bit words on a 64-bit build, two 32-bit words on a 32-bit one. SO_TIMESTAMPNS is the
    legacy option, so on 32-bit ARM the kernel deliberately hands back the old 8-byte
    `timespec32` — and 32-bit Raspberry Pi OS is the default image for every Pi that is not a 4
    or a 5, which is most of the ones a nexmon_csi build ends up on.

    Demanding 16 bytes there means every frame falls through to the caller's wall-clock
    fallback. That does not break anything visibly: the timestamps are still monotonic and still
    roughly right, but they are stamped in this process instead of in the driver, so they now
    carry its scheduling jitter — and the breathing estimator resamples on exactly these
    timestamps and cannot repair one that is wrong. A degradation you cannot see is the worst
    shape for this particular field to fail in, which is why the caller warns about the
    fallback at all.

    So take the width from the message rather than assuming it. Both layouts are a pair of
    signed native words; only the width of the word differs, and the message length says which.
    """
    for level, ctype, data in ancdata:
        if level != socket.SOL_SOCKET or ctype != SCM_TIMESTAMPNS:
            continue
        fmt = _TIMESPEC_FORMATS.get(len(data))
        if fmt is None:
            return None
        sec, nsec = struct.unpack(fmt, data)
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


class MulticastStimulus(threading.Thread):
    """Emits multicast on the wired interface so the access point has something to transmit.

    For a Pi whose radio does nothing but listen, this is the only way to make the capture rate
    a property of the node. The packet leaves over Ethernet, the access point floods it onto
    every BSS it bridges, and the monitor radio measures the access point's transmission of it.
    Nothing has to be associated on the wireless side, and no second device has to exist —
    which is what separates this from stimulating the channel through another station.

    What it costs is width. Broadcast and multicast go out at the basic rate: legacy OFDM,
    20 MHz. So an 80 MHz capture that would be 256 subcarriers of household traffic becomes 64
    subcarriers while this is running. That is why it is a fallback rather than the default, and
    why `RateGate` only arms it when there is nothing better to measure.

    Armed and disarmed from the capture loop rather than starting and stopping, so that a
    transition costs an Event set instead of a thread and a socket.
    """

    def __init__(self, iface: str, group: str, port: int, hz: float) -> None:
        super().__init__(name="stimulus", daemon=True)
        self.iface = iface
        self.group = group
        self.port = port
        self.interval = 1.0 / hz if hz > 0 else 0.0
        self.sent = 0
        self.failures = 0
        self.failed = False
        self._armed = threading.Event()
        self._stop = threading.Event()

    def arm(self, on: bool) -> None:
        if on:
            self._armed.set()
        else:
            self._armed.clear()

    def stop(self) -> None:
        self._stop.set()
        self._armed.set()

    def _open(self) -> socket.socket:
        """A socket pinned to the wired interface.

        Pinned twice over, because the one mistake this feature cannot survive is the packets
        leaving somewhere else — they would be sent perfectly, counted as sent, and never touch
        the air. IP_MULTICAST_IF is the option that actually governs multicast egress;
        SO_BINDTODEVICE also settles the source address and stops a default route on another
        interface from winning. `Prober` shipped with this bug in the other direction, sending
        every probe out of eth0 when it wanted wlan0, and it looked exactly like a dead radio.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # struct ip_mreqn { in_addr imr_multiaddr; in_addr imr_address; int imr_ifindex; } —
        # selecting by index rather than by address, so this needs no IP on the interface.
        index = socket.if_nametoindex(self.iface)
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            struct.pack("@4s4si", b"\x00" * 4, b"\x00" * 4, index),
        )
        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, self.iface.encode() + b"\x00")
        # One hop. This is for the local segment and has no business past the first router.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        return sock

    def run(self) -> None:
        try:
            sock = self._open()
        except OSError as exc:
            log.error("cannot emit the stimulus on %s: %s", self.iface, exc)
            self.failed = True
            return

        target = (self.group, self.port)
        due = time.monotonic()
        while not self._stop.is_set():
            if not self._armed.is_set():
                self._armed.wait(0.2)
                # Restart the schedule rather than catching up on everything not sent while
                # disarmed, which would arrive as one burst.
                due = time.monotonic()
                continue
            try:
                sock.sendto(STIMULUS_PAYLOAD, target)
                self.sent += 1
            except OSError:
                self.failures += 1

            due += self.interval
            now = time.monotonic()
            if due < now:
                due = now
            self._stop.wait(due - now)
        sock.close()


class RateGate:
    """Decides whether the stimulus should be running, from how busy the channel already is.

    Kept free of sockets and of the clock so the whole policy can be tested directly.

    The input is the count of *full-width* frames seen, not of all frames. That distinction is
    what stops the gate oscillating: once armed, the stimulus produces narrowband frames, and a
    gate that counted those would immediately decide the channel was busy, disarm, watch the
    rate collapse and arm again. Full-width frames are the ones this node did not cause, so they
    are the only honest answer to "is anyone else using this channel".

    Two guards against flapping on top of that. The ceiling sits above the floor, so the rate
    has to travel a real distance to change the decision. And no decision stands for less than
    `dwell_s`, because each transition changes the subcarrier count, which the server answers by
    dropping the node's history — cheap once in a while, ruinous every few seconds.
    """

    def __init__(
        self, *, floor_hz: float, ceiling_hz: float, window_s: float, dwell_s: float
    ) -> None:
        self.floor_hz = floor_hz
        self.ceiling_hz = ceiling_hz
        self.window_s = window_s
        self.dwell_s = dwell_s

        self.armed = False
        self.changes = 0
        self.rate = 0.0

        self._seen = 0
        self._window_end: float | None = None
        self._next_change = 0.0

    def update(self, now: float, wideband_seen: int) -> bool:
        """Called on every wakeup with the node's running full-width frame count."""
        if self._window_end is None:
            self._window_end = now + self.window_s
            self._next_change = now
            self._seen = wideband_seen
            return self.armed
        if now < self._window_end:
            return self.armed

        self.rate = (wideband_seen - self._seen) / self.window_s
        self._seen = wideband_seen
        self._window_end = now + self.window_s

        if now < self._next_change:
            return self.armed
        if not self.armed and self.rate < self.floor_hz:
            self.armed = True
        elif self.armed and self.rate >= self.ceiling_hz:
            self.armed = False
        else:
            return self.armed

        self.changes += 1
        self._next_change = now + self.dwell_s
        return self.armed


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
        self.dropped_class = 0
        # Every full-width frame seen, forwarded or not. RateGate reads it.
        self.wideband_seen = 0

        # Which class of frame this node is currently forwarding. False is full width — real
        # traffic. True is the 20 MHz world the Ethernet stimulus lives in. The capture loop
        # flips it; see RateGate.
        self.narrowband = False

        self._link: tuple[bytes, int, int] | None = None
        self._t0_us: int | None = None

    def on_packet(self, pkt: NexmonPacket, capture_us: int) -> bytes | None:
        """One nexmon packet in, one uplink datagram out (or None if it is not ours)."""
        # One UDP packet arrives per configured core and spatial stream. Taking more than one
        # would emit several frames sharing a moment, which reads as duplicate sequence
        # numbers or as an impossible rate depending on how they are numbered.
        if pkt.core != self.core or pkt.spatial != self.spatial:
            self.skipped_stream += 1
            return None

        # A 20 MHz capture is a single block with nothing to compare it against, so there are no
        # classes to be in and no width to lose by stimulating. Everything is forwarded whatever
        # mode this node believes it is in — without this, `--stimulus always` on a 20 MHz link
        # would drop every frame it captured — and nothing counts as somebody else's traffic,
        # because none of it can be told apart from our own. The gate then sees silence and
        # leaves the stimulus on, which at this width is the right answer.
        splittable = pkt.csi.size >= 2 * NARROWBAND_BLOCK

        span = occupied_span(pkt.csi)
        if span is None and splittable:
            # Counted whether or not it is forwarded. "Is anyone else using this channel" is the
            # question the gate is asking, and a full-width frame is the only honest answer —
            # the narrowband ones may well be this node's own stimulus coming back.
            self.wideband_seen += 1
        if splittable and (span is not None) != self.narrowband:
            # The wrong class for the mode this node is in. Forwarding both would flip n_sub
            # from frame to frame, and the server answers a subcarrier-mask change by throwing
            # the node's history away — so the mask has to be a property of the mode rather than
            # of whichever frame happened to arrive.
            self.dropped_class += 1
            return None

        csi = pkt.csi if span is None else pkt.csi[span]

        # n_sub belongs in the link key for the same reason the transmitter does: a change of
        # width is a change of what is being measured, and every baseline built on the old width
        # describes a different instrument. At 20 MHz both modes yield 64 subcarriers, so
        # switching there changes nothing and the epoch correctly stays put.
        link = (pkt.src_mac, pkt.chanspec, csi.size)
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
                "link changed (%s ch%d %d sub -> %s ch%d %d sub); epoch now %d",
                _fmt_mac(self._link[0]),
                chanspec_channel(self._link[1]),
                self._link[2],
                _fmt_mac(pkt.src_mac),
                chanspec_channel(pkt.chanspec),
                csi.size,
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
            # The chanspec's own channel either way, which for a trimmed frame is the centre of
            # the block the chip is tuned to rather than the 20 MHz the frame actually used.
            # Coarse on purpose — see occupied_span on why the block index is not a channel.
            channel=chanspec_channel(pkt.chanspec),
            sec_channel=(
                SEC_CHANNEL_NONE if span is not None else chanspec_sec_channel(pkt.chanspec)
            ),
            data=quantize(csi),
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

    # A blocking recvmsg parks this thread until the next frame arrives, and "no frames are
    # arriving" is both the condition the stimulus exists to break and the moment a status line
    # is worth the most. Neither can happen from inside a blocked read, so wake up anyway.
    capture.settimeout(CAPTURE_WAKEUP_S)

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

    stimulus = None
    gate = None
    if args.stimulus != "off" and args.stimulus_hz > 0:
        stimulus = MulticastStimulus(
            args.stimulus_iface, args.stimulus_group, args.stimulus_port, args.stimulus_hz
        )
        stimulus.start()
        if args.stimulus == "always":
            node.narrowband = True
            stimulus.arm(True)
            log.info(
                "stimulus: %s -> %s:%d at %g Hz, always on; measuring at 20 MHz",
                args.stimulus_iface, args.stimulus_group, args.stimulus_port, args.stimulus_hz,
            )
        else:
            gate = RateGate(
                floor_hz=args.stimulus_floor_hz,
                ceiling_hz=args.stimulus_ceiling_hz,
                window_s=args.stimulus_window,
                dwell_s=args.stimulus_dwell,
            )
            log.info(
                "stimulus: %s -> %s:%d at %g Hz, arming below %g Hz of real traffic",
                args.stimulus_iface, args.stimulus_group, args.stimulus_port,
                args.stimulus_hz, args.stimulus_floor_hz,
            )

    log.info(
        "node %d: %s -> %s:%d, core %d stream %d",
        args.node_id, args.iface, args.server, args.port, args.core, args.spatial,
    )

    bad = 0
    unstamped = 0
    n_sub = 0
    reported = time.monotonic()
    try:
        while True:
            try:
                packet, ancdata, _flags, _addr = capture.recvmsg(4096, socket.CMSG_SPACE(64))
            except TimeoutError:
                packet, ancdata = None, ()

            now = time.monotonic()
            stats.refresh(now)
            node.rssi, node.noise = stats.rssi, stats.noise

            payload = udp_payload(packet, NEXMON_UDP_PORT) if packet is not None else None
            if payload is not None:
                pkt = parse_nexmon(payload)
                if pkt is None:
                    bad += 1
                else:
                    capture_us = timestamp_us(ancdata)
                    if capture_us is None:
                        # No kernel timestamp: fall back, but say so, because this silently
                        # degrades the breathing estimate rather than breaking anything visibly.
                        capture_us = time.clock_gettime_ns(time.CLOCK_REALTIME) // 1000
                        if unstamped == 0:
                            log.warning(
                                "no SO_TIMESTAMPNS; timestamps will carry scheduling jitter"
                            )
                        unstamped += 1

                    datagram = node.on_packet(pkt, capture_us)
                    if datagram is not None:
                        # From the datagram rather than from the packet, because a trimmed frame
                        # carries fewer subcarriers than the one that arrived and the operator
                        # wants to know what is actually being sent.
                        n_sub = (len(datagram) - _WIRE_HEADER.size) // 2
                        out.sendto(datagram, server)

            if gate is not None:
                armed = gate.update(now, node.wideband_seen)
                if armed != node.narrowband:
                    node.narrowband = armed
                    stimulus.arm(armed)
                    log.info(
                        "%s the stimulus: %.1f Hz of real traffic; measuring at %s from here",
                        "arming" if armed else "disarming",
                        gate.rate,
                        "20 MHz" if armed else "full width",
                    )

            if args.status_interval > 0 and now - reported >= args.status_interval:
                rate = node.frames / max(now - reported, 1e-9)
                extra = ""
                if stimulus is not None:
                    extra = ", stimulus %s (%d sent)" % (
                        "on" if node.narrowband else "off", stimulus.sent,
                    )
                log.info(
                    "%d frames (%.1f Hz), %d subcarriers, rssi %d dBm, %d link changes, "
                    "%d malformed, %d unstamped, %d off-class%s",
                    node.frames, rate, n_sub, node.rssi, node.link_changes, bad, unstamped,
                    node.dropped_class, extra,
                )
                if (
                    stimulus is not None
                    and node.narrowband
                    and stimulus.sent > 0
                    and node.frames == 0
                ):
                    # Emitting into a void. Worth a warning of its own because everything above
                    # looks healthy — the packets really are being sent — and the fault is a
                    # switch or access point silently declining to put them on the air.
                    log.warning(
                        "%d stimulus packets sent and nothing came back. The access point is "
                        "probably not flooding %s onto the wireless side: check that %s is "
                        "bridged to the same network as the AP, that the group is inside "
                        "224.0.0.0/24, and that the monitor radio is on the AP's channel.",
                        stimulus.sent, args.stimulus_group, args.stimulus_iface,
                    )
                node.frames = 0
                reported = now
    except KeyboardInterrupt:
        pass
    finally:
        if prober is not None:
            prober.stop()
        if stimulus is not None:
            stimulus.stop()
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
    p.add_argument("--stimulus",
                   default=os.environ.get("CSI_STIMULUS", "auto"),
                   choices=("auto", "always", "off"),
                   help="Ethernet multicast stimulus: arm it when the channel goes quiet "
                        "(auto), run it unconditionally (always), or never (off)")
    p.add_argument("--stimulus-iface",
                   default=os.environ.get("CSI_STIMULUS_IFACE", "eth0"),
                   help="wired interface to emit on (default eth0)")
    p.add_argument("--stimulus-group",
                   default=os.environ.get("CSI_STIMULUS_GROUP", STIMULUS_GROUP),
                   help=f"multicast group (default {STIMULUS_GROUP}; stay inside 224.0.0.0/24 "
                        "or a snooping access point will prune it)")
    p.add_argument("--stimulus-port", type=int,
                   default=int(os.environ.get("CSI_STIMULUS_PORT", str(STIMULUS_PORT))),
                   help=f"multicast port (default {STIMULUS_PORT})")
    p.add_argument("--stimulus-hz", type=float,
                   default=float(os.environ.get("CSI_STIMULUS_HZ", "50")),
                   help="emission rate while armed, 0 to disable (default 50)")
    p.add_argument("--stimulus-floor-hz", type=float,
                   default=float(os.environ.get("CSI_STIMULUS_FLOOR_HZ", "10")),
                   help="arm below this many full-width frames per second (default 10)")
    p.add_argument("--stimulus-ceiling-hz", type=float,
                   default=float(os.environ.get("CSI_STIMULUS_CEILING_HZ", "25")),
                   help="disarm above this many (default 25; must exceed the floor)")
    p.add_argument("--stimulus-window", type=float, default=5.0,
                   help="seconds the gate averages the frame rate over (default 5)")
    p.add_argument("--stimulus-dwell", type=float, default=30.0,
                   help="minimum seconds between gate decisions (default 30)")
    p.add_argument("--rssi-interval", type=float, default=1.0,
                   help="seconds between /proc/net/wireless reads (default 1)")
    p.add_argument("--status-interval", type=float, default=10.0,
                   help="seconds between status lines, 0 to disable (default 10)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if not 1 <= args.node_id <= 254:
        p.error("--node-id must be 1..254; 0 and 255 are reserved")
    # argparse checks `choices` for values it parsed off the command line, but not for a default
    # — and this default comes from /etc/default/csi-node, which is hand-edited. Left unchecked,
    # `CSI_STIMULUS=Auto` reads as neither "always" nor "off" and lands silently in gated mode.
    if args.stimulus not in ("auto", "always", "off"):
        p.error(f"--stimulus must be auto, always or off (got {args.stimulus!r})")
    # Equal bounds would make the gate flip on every window; inverted ones would latch. Both
    # are configuration mistakes that look like a working node until you read the epoch count.
    if args.stimulus_ceiling_hz <= args.stimulus_floor_hz:
        p.error("--stimulus-ceiling-hz must exceed --stimulus-floor-hz; the gap is the "
                "hysteresis that stops the gate oscillating")
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
