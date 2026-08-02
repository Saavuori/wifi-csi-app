#!/usr/bin/env python3
"""Raspberry Pi CSI node: nexmon_csi capture -> the uplink wire format.

The Pi is a third kind of node alongside the two ESP32 roles. nexmon_csi patches the BCM43455
firmware to hand CSI to the host as UDP broadcasts on port 5500; this turns those into the
`docs/wire-format.md` uplink datagrams the server already ingests, so nothing downstream knows
it is looking at a Pi.

What it is *not* is a station probing its access point. That is the ESP32 topology and this
file used to claim it here; on this hardware it is impossible. The extractor's ucode deafens
the receiver for the duration of every CSI dump, so while it is armed the onboard radio cannot
hold a link that carries traffic — measured in associated mode: DHCP never completes, pings
never arrive, and the CSI that does come back trickles in at ~17 Hz. Upstream considers this
intended, the patch being written for monitor mode.

So the onboard BCM43455 sits in monitor mode and does nothing but listen, and a USB dongle
holds the association, the route into this machine and the probe traffic. The two radios are
independent: the capture interface can watch any channel while the dongle stays associated
wherever it likes, which is what makes choosing a transmitter safe on a machine reachable only
over that dongle.

That changes what a sample is, in two ways this file has to answer for. A monitor receiver is
handed every frame the chosen transmitter sends to *anyone*, so the rate is the whole
household's traffic to that access point rather than a property of this node — measured 201 Hz
with no probes at all against 215 Hz with 200 Hz of them, because one block ack covers a whole
aggregated burst. `--max-hz` is what the surplus is spent on. And those frames are not one
population: block acks, QoS data and beacons measure visibly different channels, so `--fctl`
and `Node._keep_class` lock onto one kind.

Some of what the wire format carries is not in a nexmon packet, and each gap is handled here:

  seq        nexmon reports the *802.11* sequence number of the frame that triggered the
             capture. That is 12 bits, wraps every 4096 frames, and belongs to the
             transmitter. Fed to the server it would wrap every ~41 s at 100 Hz and read as a
             reboot. We mint our own monotonic counter instead.
  timestamp  no radio-level timestamp exists. We use the kernel's receive timestamp
             (SO_TIMESTAMPNS), which is stamped in the driver rather than in this process, so
             it does not carry our scheduling jitter. This matters: the breathing estimator
             resamples on these timestamps and cannot repair one that is wrong.
  rssi       this build carries one per frame — a signed byte between the magic and the MAC —
             and it is the only one available. In monitor mode the interface is not associated,
             so /proc/net/wireless lists nothing for it and the driver's estimate reads as a
             confident zero forever. LinkStats stays as the fallback for a build that leaves
             the byte empty. Either way the server only uses RSSI through a 30-second EMA (see
             server/csi/dsp/preprocess.py), so per-frame precision was never the point.
  channel    decoded from the chanspec in the packet.
  link_epoch nexmon has no notion of one, but it reports the transmitter and the chanspec of
             every frame, and a change in either is exactly what the epoch exists to signal.

src_mac is the one v2 field that arrives for free: it is in every nexmon packet. In monitor
mode it is whichever transmitter the extractor was pointed at, which need not be an access
point this machine is associated to — usually it deliberately is not.

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
# magic u16 | rssi i8 | fctl u8 | src_mac 6 | seq u16 | core/spatial u16 | chanspec u16 |
# chip u16  =  18 bytes.
#
# This was 16 for a long time, with src_mac read from offset 2, and it rejected every real
# frame: a bcm43455c0 at 80 MHz emits 1042 bytes, so a 16-byte header leaves 1026, and
# 1026 % 4 == 2 fails the "int16 pair per subcarrier" check in parse_nexmon. Nothing said so,
# because the malformed-packet branch `continue`s past the status line that counts it.
#
# The rssi/fctl pair between the magic and the MAC is what was missing. Verified against a
# live capture: with 18, src_mac reads back as the associated BSSID and chanspec matches what
# `nexutil -k` reports for the same link, and (1042 - 18) / 4 = 256 subcarriers.
NEXMON_HEADER_SIZE = 18
_OFF_RSSI = 2
_OFF_FCTL = 3
_OFF_SRC_MAC = 4
_OFF_SEQ = 10
_OFF_CORE_SPATIAL = 12
_OFF_CHANSPEC = 14

ETH_P_IP = 0x0800
IPPROTO_UDP = 17
NEXMON_UDP_PORT = 5500

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

    __slots__ = ("src_mac", "chanspec", "core", "spatial", "csi", "rssi", "fctl")

    def __init__(self, src_mac: bytes, chanspec: int, core: int, spatial: int, csi: np.ndarray,
                 rssi: int = 0, fctl: int = 0):
        self.src_mac = src_mac
        self.chanspec = chanspec
        self.core = core
        self.spatial = spatial
        self.csi = csi
        self.rssi = rssi
        self.fctl = fctl

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

    src_mac = payload[_OFF_SRC_MAC:_OFF_SRC_MAC + 6]
    core_spatial = struct.unpack_from("<H", payload, _OFF_CORE_SPATIAL)[0]
    chanspec = _plausible_chanspec(payload, _OFF_CHANSPEC)

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
        # This build does carry a per-frame RSSI, one signed byte between the magic and the MAC.
        # It is the only signal-strength number available here: in monitor mode the interface is
        # not associated, so /proc/net/wireless has no entry for it and reports zero forever.
        rssi=struct.unpack_from("<b", payload, _OFF_RSSI)[0],
        # First byte of the 802.11 frame control field, which is what kind of frame produced this
        # measurement. Frames of different kinds are not interchangeable samples -- see
        # Node._keep_class.
        fctl=payload[_OFF_FCTL],
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
# What the 95th percentile of a frame's magnitudes is scaled to, not what the maximum is. The
# gap up to 127 is deliberate headroom for the genuine peaks above that percentile.
QUANT_PEAK = 90.0


def quantize(csi: np.ndarray) -> np.ndarray:
    """complex64[n_sub] -> int8[2*n_sub], interleaved (imag, real).

    Scaled per frame so the largest component lands near full scale. That discards the frame's
    absolute gain, which sounds worse than it is: the server's normalization divides by the
    frame's own RMS in both of its default modes, so absolute scale was going to be discarded
    anyway, and int8 is what the ESP32 delivers — the whole DSP was built and tuned on it.

    What it does cost is the AGC step detector in preprocess.py, which looks for a uniform
    frame-to-frame amplitude jump. Per-frame scaling removes the jump before the server sees
    it, so that detector will not fire on Pi frames. The step is gone rather than flagged.

    Scaled against a high percentile rather than the maximum, and clipped. The maximum is not a
    robust statistic here: this extractor puts a handful of bins far outside the band's real
    dynamic range — measured on a 20 MHz capture, a median of 694 against 10276 and 34691 at
    indices 28 and 29, in the guard region the server's own layout already marks invalid. Scaling
    to the largest of those pushed the entire real signal down to about 2 counts of 127, so
    nearly all of the int8 range was spent on two bins nothing reads, and the waterfall showed a
    bright line with darkness either side. A percentile ignores them, the real band gets most of
    the range, and clipping keeps the outliers from wrapping.
    """
    mag = np.abs(csi)
    # High enough to leave genuine peaks alone -- a real channel's strongest subcarrier should
    # still land above the reference and simply clip a little -- and low enough that a few
    # decorated bins cannot drag it. Headroom below 127 is what absorbs the difference.
    ref = float(np.percentile(mag, 95)) if csi.size else 0.0
    scale = QUANT_PEAK / ref if ref > 0.0 else 0.0

    out = np.empty(2 * csi.size, dtype=np.int8)
    out[0::2] = np.clip(np.rint(csi.imag * scale), -127, 127).astype(np.int8)
    out[1::2] = np.clip(np.rint(csi.real * scale), -127, 127).astype(np.int8)
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

    A fallback, not the main path. The capture interface runs in monitor mode and is therefore
    not associated, so /proc/net/wireless does not list it at all and this reports zero forever
    — which is exactly why `Node.on_packet` prefers the per-frame RSSI in the nexmon header and
    only falls back here when that byte does not read like a dBm.

    Sampled slowly either way. The server's hybrid normalization feeds RSSI through a 30-second
    EMA before using it, so a per-frame value would buy nothing, and reading /proc at the frame
    rate would cost real CPU on a Pi.
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

# How long the capture socket blocks before the main loop goes round anyway. Short enough that
# a probe audit's quiet window ends on time on a link where nothing at all is arriving, long
# enough to cost nothing when frames are.
CAPTURE_POLL_S = 0.5


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


# A probe wants to be a normal-sized frame rather than a runt, and small enough to be invisible
# on any link. Nothing reads the contents; the marker is there so that anyone looking at a packet
# capture of their own network can see at a glance what these are and where they come from.
PROBE_PAYLOAD = b"CSI-PROBE" + bytes(23)

# Unregistered, and nothing on a home network listens there. Probes are one-way -- no reply is
# needed, so an unreachable port is not merely acceptable but preferable.
PROBE_PORT = 5599

# How long a paused prober waits between checks that it may transmit again. Resuming does not
# wait for it -- the event wakes the thread -- so this only bounds how late a stop arrives.
PROBE_PAUSE_POLL_S = 0.1

SIOCGIFBRDADDR = 0x8919


def interface_broadcast(iface: str) -> str | None:
    """The broadcast address configured on `iface`, or None if it has none."""
    try:
        import fcntl
    except ImportError:  # not Linux; the tests import this module anywhere
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(sock.fileno(), SIOCGIFBRDADDR,
                             struct.pack("256s", iface.encode()[:15]))
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None
    finally:
        sock.close()


class Prober(threading.Thread):
    """Makes the radio being measured transmit, so that there is something to measure.

    What is being bought here is a floor. In monitor mode the extractor dumps every frame the
    chosen transmitter sends to anyone, so most of what arrives is the household's own traffic to
    that access point -- and at four in the morning that is nothing at all. Without a floor the
    instrument is only as good as the neighbours' browsing habits.

    How the floor is generated matters more than how hard. Three modes, in order of how much they
    are worth, all measured on this hardware against a 4 Hz quiet-night baseline:

      broadcast  UDP to the interface's broadcast address. The access point has to acknowledge
                 every frame we send it, and a broadcast is not aggregated into someone else's
                 A-MPDU, so the acknowledgement arrives one-for-one: 100 Hz sent bought 78 Hz
                 captured, 300 Hz bought 166 Hz. It needs no target and no reply, which is why
                 it is the default.
      unicast    UDP at a named host. Worth using when that host is a wireless client of the
                 access point being measured, because then the access point must also put the
                 packet itself on the air, addressed to that client. Needs to know the host.
      icmp       what this used to do: ping a host and measure the reply. Much the weakest --
                 200 Hz of probes bought about 14 Hz -- because the replies come back through a
                 remote IP stack that rate-limits them and aggregates what is left. Kept because
                 it is the only one that proves round-trip reachability.

    Whichever mode, the probes have to leave by a radio that can transmit, which is never the
    capture interface (see --probe-iface). Note also what none of this can do: an access point
    sends group-addressed frames at a legacy basic rate, and this extractor only produces CSI for
    HT/VHT frames, so provoking an access point to *broadcast* yields nothing. What works is
    making it transmit to a station -- ourselves, by acknowledgement, or a client, by unicast.
    """

    MODES = ("broadcast", "unicast", "icmp")

    def __init__(self, target: str | None, hz: float, iface: str | None = None,
                 mode: str = "broadcast", port: int = PROBE_PORT) -> None:
        super().__init__(name="prober", daemon=True)
        # A comma-separated target is a list of destinations to rotate through, and it is the
        # only way to raise the rate of *wide* frames. 802.11 aggregation is per receiver: an
        # access point batches everything queued for one station into a single transmission, and
        # CSI is produced per transmission rather than per packet. So one destination is capped
        # at whatever that station's traffic happens to provoke -- measured here, 25 Hz, and
        # unmoved by anything from 30 to 800 packets a second or by 140 Mbit of flooding. Each
        # additional destination is a separate queue and therefore separate transmissions:
        # measured on the same access point, 1 destination gave 26 Hz and 14 gave 98 Hz, all of
        # them 80 MHz wide.
        self.targets = [t.strip() for t in target.split(",") if t.strip()] if target else []
        self.target = self.targets[0] if self.targets else None
        self.iface = iface
        self.mode = mode
        self.port = port
        self.interval = 1.0 / hz if hz > 0 else 0.0
        self.sent = 0
        self.failures = 0
        self._stop = threading.Event()
        # Set means "transmit". Cleared by ProbeAudit for a few seconds at a time; a thread
        # cannot be restarted, and reopening the socket every audit would make the audit itself
        # a variable, so the pause happens inside the send loop instead.
        self._active = threading.Event()
        self._active.set()

    def stop(self) -> None:
        self._stop.set()
        # Or a thread parked in the pause below waits out its poll before noticing.
        self._active.set()

    def pause(self) -> None:
        """Stop transmitting, without stopping the thread. Idempotent."""
        self._active.clear()

    def resume(self) -> None:
        self._active.set()

    @property
    def probing(self) -> bool:
        return self._active.is_set()

    def _open(self) -> socket.socket | None:
        if self.mode == "icmp":
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            except PermissionError:
                log.error("icmp probing needs root; use --probe-mode broadcast or --probe-hz 0")
                return None
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if self.mode == "broadcast":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Pin the probes to the interface we are measuring. Without this the kernel picks by
        # route, and on a Pi with Ethernet plugged in as well the gateway is reachable both
        # ways -- so every probe leaves via eth0, puts nothing on the air, and the capture
        # rate collapses to the beacon rate with no error anywhere to explain it. Wrong in a
        # way that looks exactly like a dead radio.
        if self.iface:
            try:
                sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE,
                                self.iface.encode() + b"\0")
            except OSError as e:
                log.warning("could not bind probes to %s (%s); they will follow the routing "
                            "table and may not traverse the radio", self.iface, e)
        sock.setblocking(False)
        return sock

    def run(self) -> None:
        sock = self._open()
        if sock is None:
            return

        ident = os.getpid() & 0xFFFF
        seq = 0
        # Absolute schedule rather than sleep(interval), so a slow send does not let the rate
        # drift downward over a long run.
        due = time.monotonic()
        while not self._stop.is_set():
            if not self._active.is_set():
                # Paused for an audit. Park here rather than spinning, and take the schedule
                # from the moment probing resumes: repaying the pause as a burst would put a
                # transient into the very rate the audit is about to measure.
                self._active.wait(PROBE_PAUSE_POLL_S)
                due = time.monotonic()
                continue
            try:
                target = self.targets[seq % len(self.targets)]
                if self.mode == "icmp":
                    sock.sendto(icmp_echo(ident, seq), (target, 0))
                else:
                    sock.sendto(PROBE_PAYLOAD, (target, self.port))
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


# -- is the probing doing anything? --------------------------------------------------------

# Wait this long before the first audit rather than a whole interval. Five minutes is too long
# to go without an answer at deployment, which is exactly when the question is live; and this is
# comfortably past the class lock's slowest close (learn_max_seconds, 30 s), before which no
# frame counts as supply at all and both halves of an audit would read zero.
PROBE_AUDIT_FIRST_WAIT_S = 60.0

# Below this share of the arriving rate, the probes are not buying anything a measurement can
# see. Not zero: the two halves are counted on traffic that is mostly not ours, so a few percent
# either way is the household, not the probes.
PROBE_AUDIT_SILENT_SHARE = 0.05

# Below this, no probe left the machine at all -- every send failed -- which is a different
# fault from probes that leave and provoke nothing, and has a different fix.
PROBE_AUDIT_SILENT_SENT_HZ = 1.0

_PROBE_ADVICE = {
    "broadcast": (
        "A broadcast is measurable here only through the access point's acknowledgement of our "
        "own uplink, which exists only when the probes leave by a radio associated to it -- "
        "from a wired uplink there is no acknowledgement to us at all. The access point's own "
        "group-addressed transmissions cannot fill the gap: those go out at a legacy basic rate "
        "and this extractor produces CSI only for HT/VHT frames, so no number of broadcasts is "
        "ever measurable that way. Try --probe-mode unicast at a wireless client of the access "
        "point named by --ap, which the access point must deliver over the air to a station."
    ),
    "unicast": (
        "Unicast probes are measurable only when --probe names a wireless client of the access "
        "point named by --ap: then the access point has to put the packet on the air, addressed "
        "to that station. A target reached over the wire, or associated to a different radio, "
        "provokes nothing this capture can see."
    ),
    "icmp": (
        "icmp is much the weakest mode -- the replies come back through a remote IP stack that "
        "rate-limits them and aggregates what is left, measured here at about 14 Hz for 200 Hz "
        "of probes. Try --probe-mode unicast at a wireless client of the access point named by "
        "--ap."
    ),
}


class ProbeAudit:
    """Stops probing now and then, to find out whether probing is doing anything.

    Everything the prober claims is site-specific. Broadcast bought 78 Hz of the 104 Hz arriving
    on the machine it was measured on, but that number is a property of one access point on one
    evening: an access point that converts multicast to unicast makes broadcast probing work, one
    that does not makes it useless, and on a wired uplink there is no acknowledgement coming back
    to us at all so it may buy nothing whatsoever. The alternative to measuring this is guessing,
    and a node that guesses wrong reports a plausible rate made entirely of the neighbours'
    browsing -- which looks like a working instrument and is not one.

    So: `interval_s` apart, count the arriving rate for `window_s` with the probes running, then
    for another `window_s` with them stopped. The difference is what the probes are buying. The
    two halves are adjacent and the same length on purpose -- household traffic drifts over
    minutes, and comparing a five-second sample against a five-minute average would attribute
    that drift to the probes.

    Two things this deliberately does *not* do. It does not touch the node: the quiet half is a
    gap in supply, not a change in what a sample is, so the sequence counter and the link epoch
    run straight through it and the server sees a lull rather than a discontinuity. And it counts
    frames before the rate limiter rather than after, because the limiter's whole job is to
    flatten a surplus -- measured after it, 104 Hz of supply and 26 Hz of supply both read as the
    100 Hz cap and the audit would find nothing while everything was working.

    The first frames of the quiet half can still be probe-induced, so the background is measured
    very slightly high and the yield very slightly low. That is the safe direction for a check
    whose purpose is to catch probing that does nothing: it errs toward a false alarm rather than
    toward false reassurance.
    """

    WAIT = "wait"
    MEASURE = "measure"
    QUIET = "quiet"

    def __init__(self, interval_s: float, window_s: float = 5.0,
                 mode: str = "broadcast", audit_file: str | None = None) -> None:
        self.audit_file = audit_file
        self.interval_s = interval_s
        self.window_s = window_s
        self.mode = mode

        self.phase = self.WAIT
        self.audits = 0
        self.total_hz: float | None = None
        self.background_hz: float | None = None
        self.sent_hz: float | None = None

        self._phase_start: float | None = None
        self._phase_frames = 0
        self._phase_sent = 0

    @property
    def enabled(self) -> bool:
        return self.interval_s > 0

    @property
    def quiet(self) -> bool:
        """Are the probes stopped right now? The status line says so, because the rate dips."""
        return self.phase == self.QUIET

    @property
    def induced_hz(self) -> float | None:
        """Frames per second the probes are responsible for. Negative means noise, not harm."""
        if self.total_hz is None or self.background_hz is None:
            return None
        return self.total_hz - self.background_hz

    @property
    def share(self) -> float | None:
        induced = self.induced_hz
        if induced is None or not self.total_hz:
            return None
        return max(0.0, induced) / self.total_hz

    def poll(self, now: float, frames: int, sent: int = 0) -> bool:
        """Advance the audit. True means the probes should be transmitting.

        `frames` is a free-running count of frames that reached the node as supply, and `sent` a
        free-running count of probes that left; only differences are used, so neither may be
        reset while an audit is in flight.
        """
        if not self.enabled:
            return True
        if self._phase_start is None:
            self._begin(self.WAIT, now, frames, sent)
            return True

        elapsed = now - self._phase_start
        if self.phase == self.WAIT:
            # Longer before the first one: see PROBE_AUDIT_FIRST_WAIT_S.
            wait = self.interval_s if self.audits else min(self.interval_s,
                                                           PROBE_AUDIT_FIRST_WAIT_S)
            if elapsed < wait:
                return True
            self._begin(self.MEASURE, now, frames, sent)
            return True

        if elapsed < self.window_s:
            return self.phase == self.MEASURE

        if self.phase == self.MEASURE:
            self.total_hz = (frames - self._phase_frames) / elapsed
            self.sent_hz = (sent - self._phase_sent) / elapsed
            self._begin(self.QUIET, now, frames, sent)
            return False

        self.background_hz = (frames - self._phase_frames) / elapsed
        self.audits += 1
        self._begin(self.WAIT, now, frames, sent)
        self._report()
        return True

    def _begin(self, phase: str, now: float, frames: int, sent: int) -> None:
        self.phase = phase
        self._phase_start = now
        self._phase_frames = frames
        self._phase_sent = sent

    def describe(self) -> str:
        if self.total_hz is None or self.background_hz is None:
            return "probe yield: not measured yet"
        share = self.share
        pct = "n/a" if share is None else f"{100 * share:.0f}%"
        return (f"probe yield: {self.induced_hz:.0f} Hz induced of {self.total_hz:.0f} Hz total "
                f"({pct}), background {self.background_hz:.0f} Hz")

    def publish(self, path: str) -> None:
        """Leave the last audit where the access-point agent can read it.

        The journal is the wrong place for this to live alone. Whether provoking traffic works at
        all is site-specific -- it depends on what the access point does with a broadcast and on
        whether the probe leaves by a radio that access point can hear -- so it is the one number
        an operator needs in front of them rather than in a log they would have to know to read.
        Written atomically, because the agent polls it on its own schedule.
        """
        if self.total_hz is None or self.background_hz is None:
            return
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="ascii") as fp:
                fp.write(f"induced_hz={self.induced_hz:.1f}\n")
                fp.write(f"total_hz={self.total_hz:.1f}\n")
                fp.write(f"background_hz={self.background_hz:.1f}\n")
                fp.write(f"mode={self.mode}\n")
                if self.sent_hz is not None:
                    fp.write(f"sent_hz={self.sent_hz:.1f}\n")
            os.replace(tmp, path)
        except OSError as exc:
            # Not worth failing a capture over; the audit is diagnostic.
            log.debug("could not write the audit to %s: %s", path, exc)

    def _report(self) -> None:
        # Published here because this is the only moment the audit is a complete answer: both
        # halves are in. Keying it off either half changing, from outside, publishes a partial
        # result -- or, when the other half was the one that moved, nothing at all.
        if self.audit_file:
            self.publish(self.audit_file)
        line = self.describe()
        if not self.total_hz and not self.background_hz:
            # Nothing at all arrived either way, so the audit has measured the capture rather
            # than the probes. Say which, or this reads as a probing fault.
            log.warning(
                "%s -- no frames arrived at all, with the probes or without them, so this says "
                "nothing about the probing. Check the capture radio, the chanspec and --ap.",
                line,
            )
            return
        share = self.share
        if share is not None and share < PROBE_AUDIT_SILENT_SHARE:
            log.warning("%s -- the probes are buying nothing measurable. %s", line, self._why())
            return
        log.info("%s", line)

    def _why(self) -> str:
        if self.sent_hz is not None and self.sent_hz < PROBE_AUDIT_SILENT_SENT_HZ:
            return (f"No probe actually left this machine ({self.sent_hz:.1f}/s sent), so every "
                    "send is failing: check --probe-iface is up and the target address is "
                    "routable from it.")
        return _PROBE_ADVICE.get(self.mode, "")


# -- the node -----------------------------------------------------------------------------

# How long a class lock is allowed to match nothing before it is given up and learned again.
# Named because the probe audit's quiet window has to stay well under it: relearning onto a
# different class bumps the link epoch, and an audit is not allowed to do that.
RELEARN_SECONDS = 20.0


class Node:
    """Turns nexmon packets into uplink datagrams.

    Kept free of sockets so the whole translation can be tested against synthetic packets.
    """

    def __init__(
        self,
        node_id: int,
        *,
        core: int = 0,
        spatial: int = 0,
        ap_mac: bytes | None = None,
        fctl: int | None = None,
        learn_frames: int = 400,
        learn_seconds: float = 2.0,
        learn_min: int = 50,
        learn_max_seconds: float = 30.0,
        relearn_seconds: float = RELEARN_SECONDS,
        max_hz: float = 0.0,
    ) -> None:
        self.node_id = node_id
        self.core = core
        self.spatial = spatial
        self.ap_mac = ap_mac

        self.seq = 0
        self.epoch = 0
        self.rssi = 0
        self.noise = 0

        self.frames = 0
        # Frames that were the measurement -- right transmitter, right class, right width --
        # whether or not the rate limiter then kept them. Free-running, and unlike the counters
        # below it is never reset by the status line, because ProbeAudit reads differences of it
        # across windows that are longer than a status interval.
        self.eligible = 0
        self.skipped_stream = 0
        self.skipped_transmitter = 0
        self.skipped_class = 0
        self.skipped_width = 0
        self.skipped_rate = 0
        self.link_changes = 0
        self.class_changes = 0

        # An upper bound on the sample rate, which is really a bound on how uneven it may be.
        #
        # What arrives is not ours to choose: the extractor dumps every frame this access point
        # sends to anyone, and measured here that background alone is ~200 Hz while our own
        # 200 Hz of probes adds only ~14 Hz -- block acks cover a whole aggregated burst, so
        # probing harder does not buy proportionally more frames. The consequence is that the
        # raw rate follows the household's traffic and wanders between roughly 100 and 300 Hz.
        #
        # Breathing lives below 0.5 Hz and motion well under 10, so none of that rate is needed.
        # Taking a frame only once per interval spends the surplus on regularity instead: the
        # spacing becomes near-uniform, which is what the server's resampler wants, and the
        # reported rate stops reporting the neighbours.
        self.min_interval_us = int(1e6 / max_hz) if max_hz > 0 else 0
        self._next_due_us: int | None = None

        # What one sample looks like: a frame kind and a subcarrier count. Both are locked --
        # either to what was configured, or to whatever dominates the opening seconds.
        self.fctl = fctl
        self._configured_fctl = fctl
        self.n_sub: int | None = None
        self._last_ok: float | None = None
        self._relearn_seconds = relearn_seconds
        self._lost_lock: tuple[int, int] | None = None
        self._class_epoch_pending = False
        # The window runs even when the class is configured. It used to be skipped, on the
        # reasoning that a configured class needs no learning -- but the width is never
        # configured, and with the window closed the very first frame decided it. One of the
        # ~1% that arrive 65 subcarriers wide would lock the node to a width that then almost
        # never recurs, and since nothing re-learns, it would reject every correct frame for the
        # life of the process while logging a confident "0 frames".
        self._learn_frames = learn_frames
        self._learn_seconds = learn_seconds
        self._learn_min = learn_min
        self._learn_max_seconds = learn_max_seconds
        self._learn: dict[tuple[int, int], int] = {}
        self._learn_t0: float | None = None

        self._link: tuple[bytes, int] | None = None
        self._t0_us: int | None = None

    def _keep_class(self, pkt: NexmonPacket, now: float) -> bool:
        """Is this frame the same kind of measurement as the others?

        A transmitter's frames are not interchangeable samples of one channel. The firmware
        filter matches on the transmitter only, so an access point's block acks, its beacons and
        its data frames all arrive together -- and they are sent at different rates, widths and
        antenna weightings, so each kind measures a visibly different channel. Interleaving them
        does not average out; it puts one stray row into the waterfall every time the rare kind
        appears, which reads as a spike in a still room.

        Measured on this link: 98.2% block acks (fctl 0x94) against 1.8% QoS data (0x88), the
        two medians correlating only 0.918 with each other while each kind self-correlates
        above 0.99. About four stray rows a second.

        Which kind to keep is not obvious in advance -- it depends on what the probe traffic
        provokes -- so unless one is configured, spend the first couple of seconds counting and
        keep whichever dominates. The subcarrier count rides along in the same lock, because a
        frame of a different width is a different layout and the server would have to relearn
        the whole subcarrier map to read it.
        """
        key = (pkt.fctl, pkt.n_sub)

        if self.fctl is not None and self.n_sub is not None:
            if key == (self.fctl, self.n_sub):
                self._last_ok = now
                return True
            if self._last_ok is None:
                self._last_ok = now
            # A lock that outlives its class is indistinguishable from a dead radio: the node
            # reports zero frames while frames are arriving. That happens for ordinary reasons --
            # the access point changes bandwidth, or whatever was provoking this kind of frame
            # stops -- so give the lock up and learn again rather than sit there silent.
            if now - self._last_ok >= self._relearn_seconds:
                log.warning(
                    "nothing of the locked class (fctl 0x%02x, %d subcarriers) for %.0fs "
                    "while frames keep arriving; learning again",
                    self.fctl, self.n_sub, now - self._last_ok,
                )
                self._lost_lock = (self.fctl, self.n_sub)
                self.fctl = self._configured_fctl
                self.n_sub = None
                self._learn.clear()
                self._learn_t0 = None
                self._last_ok = None
            return False

        # Still learning. Nothing is emitted meanwhile: the server calibrates its baseline on
        # the first frames it sees, and a baseline built from a mixture describes no channel.
        if self._learn_t0 is None:
            self._learn_t0 = now
        self._learn[key] = self._learn.get(key, 0) + 1

        total = sum(self._learn.values())
        elapsed = now - self._learn_t0
        # Enough frames, or enough time *and* enough frames to mean anything. The elapsed test
        # alone decided it once on a sample of three, when a hand-picked radio turned out to have
        # almost no traffic: a majority of three is not a majority. Sparse links are exactly where
        # a wrong lock is most expensive, because there is nothing arriving to reveal the mistake.
        if total < self._learn_frames:
            if elapsed < self._learn_seconds or total < self._learn_min:
                # ...but not forever. A link that never reaches the minimum still has to be
                # measured, so give up waiting eventually and say the sample was thin.
                if elapsed < self._learn_max_seconds:
                    return False
                log.warning(
                    "locking on a thin sample: only %d frames in %.0fs", total, elapsed,
                )

        # When the class was configured, the width has to be the majority *within that class*.
        # Taking the overall majority would read the width off whatever else is on the air.
        candidates = ({k: c for k, c in self._learn.items() if k[0] == self.fctl}
                      if self.fctl is not None else self._learn)
        if not candidates:
            # The window expired without a single frame of the class we were told to keep. That
            # is a mistyped --fctl or a link that does not carry it; locking now would pick a
            # width out of the air. Keep listening and say so, once per window.
            log.warning(
                "no frames of the configured class fctl 0x%02x in %.1fs (%d frames of %s); "
                "still waiting", self.fctl, elapsed, total,
                ", ".join(f"0x{f:02x}" for f, _ in sorted(self._learn)) or "nothing",
            )
            self._learn.clear()
            self._learn_t0 = None
            return False

        best = max(candidates, key=lambda k: candidates[k])
        share = candidates[best] / max(total, 1)
        self.fctl = best[0]
        self.n_sub = best[1]
        log.info(
            "measuring frame class fctl 0x%02x at %d subcarriers (%.0f%% of %d frames in %.1fs); "
            "other classes seen: %s",
            self.fctl, self.n_sub, 100 * share, total, elapsed,
            ", ".join(f"0x{f:02x}/{n}sub x{c}" for (f, n), c in
                      sorted(self._learn.items(), key=lambda kv: -kv[1])[1:4]) or "none",
        )
        self._learn.clear()
        self._last_ok = now
        # Locking onto a different kind of frame than before means the next sample is not
        # comparable with the last: the classes measure visibly different channels (their median
        # profiles correlate 0.918 here, against better than 0.99 within a class). Every baseline
        # built on the old class describes the old measurement, so this is a link change in
        # everything but name and gets the same answer -- bump the epoch and let the server throw
        # its history away. Not on the first lock: there is nothing behind it to invalidate.
        if self._lost_lock is not None and self._lost_lock != (self.fctl, self.n_sub):
            self._class_epoch_pending = True
        self._lost_lock = None
        return (pkt.fctl, pkt.n_sub) == (self.fctl, self.n_sub)

    def on_packet(self, pkt: NexmonPacket, capture_us: int,
                  now: float | None = None) -> bytes | None:
        """One nexmon packet in, one uplink datagram out (or None if it is not ours)."""
        # One UDP packet arrives per configured core and spatial stream. Taking more than one
        # would emit several frames sharing a moment, which reads as duplicate sequence
        # numbers or as an impossible rate depending on how they are numbered.
        if pkt.core != self.core or pkt.spatial != self.spatial:
            self.skipped_stream += 1
            return None

        # Keep to one transmitter. The firmware has a -m filter for this, but it is not
        # enforced in every build -- on bcm43455c0 7.45.189 in monitor mode it passes
        # everything on the channel -- and without a filter somewhere the src_mac changes
        # with almost every frame, so link_epoch climbs forever and the server throws the
        # node's history away as fast as it accumulates. Measuring two transmitters as one
        # link is meaningless anyway: they are different paths through the room.
        if self.ap_mac is not None and pkt.src_mac != self.ap_mac:
            self.skipped_transmitter += 1
            return None

        # One kind of frame, one width. See _keep_class.
        was_locked = self.fctl is not None and self.n_sub is not None
        if not self._keep_class(pkt, time.monotonic() if now is None else now):
            if was_locked:
                if pkt.n_sub != self.n_sub:
                    self.skipped_width += 1
                else:
                    self.skipped_class += 1
            return None

        # Supply, counted before the rate limiter rather than after it. The limiter exists to
        # flatten a surplus, so measured downstream of it a link carrying 104 Hz and one carrying
        # 26 Hz both read as the 100 Hz cap -- and ProbeAudit, which subtracts one from the
        # other, would find nothing at the exact moment everything was working. One increment;
        # the audit does the rest off the hot path.
        self.eligible += 1

        # Charged here rather than where the re-lock happens, so that the epoch advances on a
        # frame the server actually receives. Bumping it against a frame that is then dropped for
        # any other reason would announce a discontinuity that never arrives.
        if self._class_epoch_pending:
            self._class_epoch_pending = False
            self.class_changes += 1
            self.epoch = (self.epoch + 1) & 0xFF
            log.info("frame class changed; epoch now %d", self.epoch)

        # Rate limit before anything with side effects, so a dropped frame costs nothing but
        # itself -- the sequence counter and the link epoch must not advance for a frame the
        # server never sees.
        # Against a deadline rather than against the last frame kept. Requiring a clear gap since
        # the last one sounds equivalent but systematically undershoots: arrivals are random, so
        # the first frame past the gap is on average half an inter-arrival late, and every one of
        # those delays is inherited by the next. Measured, a 120 Hz limit settled at 85. A
        # deadline that advances by exactly one interval spends that slack instead of compounding
        # it, and the configured number is the number that comes out.
        if self.min_interval_us:
            if self._next_due_us is None:
                self._next_due_us = capture_us
            if capture_us < self._next_due_us:
                self.skipped_rate += 1
                return None
            self._next_due_us += self.min_interval_us
            # Supply is bursty, so the deadline routinely ends up slightly in the past. Snapping
            # it forward every time looks harmless and is not: each snap forfeits the credit for
            # the frames that gap cost, so the output rate follows the supply rather than the
            # setting -- measured 80 Hz against a 120 Hz limit while ~158 Hz was arriving. Let it
            # run behind and catch up, which is what holds the average at the number configured.
            #
            # Only up to a point. After a real outage the deadline would be minutes behind and
            # catching up would mean emitting everything that arrives at full speed, so past a
            # few intervals give the credit up and start again from now.
            if capture_us - self._next_due_us > 3 * self.min_interval_us:
                self._next_due_us = capture_us + self.min_interval_us

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

        # Prefer the frame's own RSSI over the driver's. In monitor mode the interface is not
        # associated, so /proc/net/wireless lists nothing for it and LinkStats reports 0 forever
        # -- which the server then feeds into its normalisation as a real measurement. Only take
        # the header value when it reads like a dBm, so a build that leaves the byte empty falls
        # back rather than reporting a confident zero.
        rssi = pkt.rssi if -110 <= pkt.rssi <= -5 else self.rssi

        datagram = encode_uplink(
            node_id=self.node_id,
            seq=self.seq,
            timestamp_us=elapsed,
            rssi=rssi,
            noise_floor=self.noise,
            channel=chanspec_channel(pkt.chanspec),
            sec_channel=chanspec_sec_channel(pkt.chanspec),
            data=quantize(pkt.csi),
            # The one v2 field nexmon answers directly: the transmitter of the frame this
            # measurement came from, which is the access point the extractor was pointed at --
            # not necessarily one this machine is associated to, and usually deliberately not.
            src_mac=pkt.src_mac,
            link_epoch=self.epoch,
        )
        self.seq += 1
        self.frames += 1
        self.rssi = rssi
        return datagram


def _fmt_mac(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


def _audit_suffix(audit: ProbeAudit | None) -> str:
    """What the status line says about the probes."""
    if audit is None or not audit.enabled:
        return ""
    suffix = "; " + audit.describe()
    if audit.quiet:
        # Said here because the rate on this very line is depressed by the audit, and a dip
        # nobody explained reads as the link failing.
        suffix += " (probes stopped for an audit, so the rate above is the background)"
    return suffix


def parse_mac(text: str | None) -> bytes | None:
    """`aa:bb:cc:dd:ee:ff` (or with dashes, or bare hex) as six bytes, or None."""
    if not text:
        return None
    raw = text.replace(":", "").replace("-", "").strip()
    if len(raw) != 12:
        raise ValueError(f"not a MAC address: {text!r}")
    return bytes.fromhex(raw)


def run(args: argparse.Namespace) -> int:
    ap_mac = parse_mac(args.ap)
    fctl = None if args.fctl in (None, "", "auto") else int(args.fctl, 0)
    node = Node(args.node_id, core=args.core, spatial=args.spatial, ap_mac=ap_mac, fctl=fctl,
                max_hz=args.max_hz)
    if args.max_hz > 0:
        log.info("limiting to %g Hz (one frame per %d us)", args.max_hz, node.min_interval_us)
    if ap_mac is not None:
        log.info("keeping only frames from %s", args.ap)
    if fctl is not None:
        log.info("keeping only frame class fctl 0x%02x", fctl)
    else:
        log.info("learning which frame class dominates before emitting")
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

    # So the loop goes round even when nothing arrives. Two things depend on it: the probe audit
    # has to be able to resume probing on a link that has gone silent -- which is precisely the
    # state its own quiet window can produce -- and a node receiving nothing at all should still
    # print a status line saying so rather than sitting mute, which is indistinguishable from a
    # crash.
    capture.settimeout(CAPTURE_POLL_S)

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server = (args.server, args.port)

    prober = None
    if args.probe_hz > 0:
        probe_iface = args.probe_iface or args.iface
        if args.probe_mode == "broadcast":
            # An explicit --probe wins, so that a host on a segment we cannot read the broadcast
            # address of can still be given one by hand. Otherwise ask the interface, and fall
            # back to the all-ones address, which needs no configuration and which the kernel
            # puts out of the bound interface regardless of the routing table.
            target = args.probe or interface_broadcast(probe_iface) or "255.255.255.255"
        else:
            target = args.probe or default_gateway()

        if target is None:
            log.warning("no probe target and no default gateway; not probing. Whatever else is "
                        "talking to this access point is still measured -- which on a busy one "
                        "is most of the rate anyway -- but a radio nobody is using will only "
                        "beacon, about 10 Hz. Pass --probe a host associated to it.")
        else:
            log.info("probing %s (%s) at %g Hz via %s",
                     target, args.probe_mode, args.probe_hz, probe_iface)
            prober = Prober(target, args.probe_hz, iface=probe_iface,
                            mode=args.probe_mode, port=args.probe_port)
            prober.start()

    audit = None
    if args.probe_audit_interval > 0:
        if prober is None:
            log.info("not auditing the probes: nothing is probing, so the whole rate is "
                     "whatever the network was carrying anyway")
        else:
            audit = ProbeAudit(args.probe_audit_interval, window_s=args.probe_audit_window,
                               mode=args.probe_mode, audit_file=args.audit_file)
            log.info("auditing the probes every %g s: %g s of arriving frames with them and "
                     "%g s without, to measure what they are actually buying",
                     args.probe_audit_interval, args.probe_audit_window,
                     args.probe_audit_window)

    log.info(
        "node %d: %s -> %s:%d, core %d stream %d",
        args.node_id, args.iface, args.server, args.port, args.core, args.spatial,
    )

    bad = 0
    bad_len = 0
    decoded = 0
    unstamped = 0
    # Width of the last frame seen, for the status line before the class lock closes -- and so
    # that a line printed before any frame at all has a number to print.
    seen_n_sub = 0
    reported = time.monotonic()

    def due_to_report(now: float) -> bool:
        return args.status_interval > 0 and now - reported >= args.status_interval

    try:
        while True:
            try:
                packet, ancdata, _flags, _addr = capture.recvmsg(4096, socket.CMSG_SPACE(64))
            except TimeoutError:
                # Nothing arrived within CAPTURE_POLL_S. Not an error -- during an audit's quiet
                # window on a quiet link it is the expected state -- but the housekeeping below
                # still has to run, which is the whole reason for the timeout.
                packet, ancdata = None, ()

            now = time.monotonic()
            payload = udp_payload(packet, NEXMON_UDP_PORT) if packet is not None else None
            pkt = parse_nexmon(payload) if payload is not None else None

            if payload is not None and pkt is None:
                bad += 1
                bad_len = len(payload)
            elif pkt is not None:
                decoded += 1
                seen_n_sub = pkt.n_sub
                stats.refresh(now)
                node.noise = stats.noise
                # Only when the driver actually has a reading. In monitor mode it never does --
                # the interface is not associated, so /proc/net/wireless does not list it and
                # LinkStats holds 0 -- and assigning that here overwrote the last good per-frame
                # value between every packet. The occasional frame whose header carries no usable
                # RSSI then fell back to a confident 0 rather than to the last real measurement,
                # and 0 dBm is not a missing value to the server: it feeds it into the
                # normalisation as a reading.
                if stats.rssi:
                    node.rssi = stats.rssi

                capture_us = timestamp_us(ancdata)
                if capture_us is None:
                    # No kernel timestamp: fall back, but say so, because this silently degrades
                    # the breathing estimate rather than breaking anything visibly.
                    capture_us = time.clock_gettime_ns(time.CLOCK_REALTIME) // 1000
                    if unstamped == 0:
                        log.warning("no SO_TIMESTAMPNS; timestamps will carry scheduling jitter")
                    unstamped += 1

                datagram = node.on_packet(pkt, capture_us, now)
                if datagram is not None:
                    out.sendto(datagram, server)

            # Off the hot path in the sense that matters: the frame path pays one counter
            # increment, and the arithmetic happens here -- two comparisons, and only on the
            # transitions is a lock taken to wake or park the prober.
            if audit is not None:
                probing = audit.poll(now, node.eligible, prober.sent)
                if probing and not prober.probing:
                    prober.resume()
                elif not probing and prober.probing:
                    prober.pause()

            if not due_to_report(now):
                continue

            if bad and not decoded:
                # For as long as *every* packet is malformed there is no rate to report and the
                # count that names the problem is the only useful thing to say. A silent node is
                # indistinguishable from a dead radio.
                log.warning(
                    "%d malformed packets, none decoded (%d bytes, header expects %d). "
                    "Is this nexmon_csi output?", bad, bad_len, NEXMON_HEADER_SIZE,
                )
                reported = now
                continue

            rate = node.frames / max(now - reported, 1e-9)
            log.info(
                "%d frames (%.1f Hz), %d subcarriers, rssi %d dBm, %d link changes, "
                "%d malformed, %d unstamped, %d other transmitters, "
                "%d other classes, %d other widths, %d over rate%s",
                node.frames, rate, node.n_sub or seen_n_sub, node.rssi, node.link_changes,
                bad, unstamped, node.skipped_transmitter,
                node.skipped_class, node.skipped_width, node.skipped_rate,
                _audit_suffix(audit),
            )
            node.frames = 0
            node.skipped_transmitter = 0
            node.skipped_class = 0
            node.skipped_width = 0
            node.skipped_rate = 0
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
    p.add_argument("--ap", default=os.environ.get("CSI_AP_MAC") or None,
                   help="only keep frames from this transmitter, e.g. aa:bb:cc:dd:ee:ff. "
                        "Required in monitor mode, where the firmware filter is not enforced "
                        "and every transmitter on the channel arrives (default: no filter)")
    p.add_argument("--max-hz", type=float, default=float(os.environ.get("CSI_MAX_HZ", "0")),
                   help="upper bound on the emitted rate, 0 for none. What arrives is set by the "
                        "access point's whole traffic load, so the raw rate follows the "
                        "household rather than the room; capping it below the quiet-hours floor "
                        "trades a surplus nothing needs for near-uniform spacing")
    p.add_argument("--fctl", default=os.environ.get("CSI_FCTL") or "auto",
                   help="keep only frames whose 802.11 frame-control byte is this, e.g. 0x94 "
                        "for block acks or 0x88 for QoS data. Frames of different kinds measure "
                        "visibly different channels, so mixing them spikes the waterfall. "
                        "'auto' (default) locks onto whichever kind dominates the first seconds")
    p.add_argument("--probe-mode", default=os.environ.get("CSI_PROBE_MODE") or "broadcast",
                   choices=Prober.MODES,
                   help="how to make the measured radio transmit. 'broadcast' (default) sends "
                        "UDP to the broadcast address and is answered by the access point's own "
                        "acknowledgements, which is the strongest floor and needs no target: "
                        "measured 100 Hz sent -> 78 Hz captured. 'unicast' sends UDP at --probe, "
                        "worth it when that host is a wireless client of the measured AP, since "
                        "the AP must then put the packet on the air as well. 'icmp' pings and "
                        "measures the reply -- much the weakest, about 14 Hz for 200 Hz of "
                        "probes, but the only one that proves round-trip reachability")
    p.add_argument("--probe-port", type=int,
                   default=int(os.environ.get("CSI_PROBE_PORT", str(PROBE_PORT))),
                   help=f"UDP port for broadcast and unicast probes (default {PROBE_PORT}); "
                        "nothing needs to be listening on it")
    p.add_argument("--probe", default=os.environ.get("CSI_PROBE_HOST") or None,
                   help="probe target, or several separated by commas (default: the interface "
                        "broadcast address in broadcast mode, the IPv4 default gateway "
                        "otherwise). Several is what raises the rate of 80 MHz frames: "
                        "mode, the IPv4 default gateway otherwise). For unicast and icmp it "
                        "must be a host associated to *the access point named by --ap*, or the "
                        "frames it provokes come from a different radio and the transmitter "
                        "filter drops every one, leaving only that AP's beacons at about 10 Hz")
    p.add_argument("--probe-iface", default=os.environ.get("CSI_PROBE_IFACE") or None,
                   help="interface to send probes from (default: the capture interface). The "
                        "capture radio is in monitor mode and cannot transmit, so point this "
                        "at the interface that holds the association -- the USB dongle, or "
                        "Ethernet. The access point then has to put every reply on the air, "
                        "and that is what gets measured")
    p.add_argument("--probe-hz", type=float,
                   default=float(os.environ.get("CSI_PROBE_HZ", "100")),
                   help="probe rate, 0 to disable (default 100)")
    p.add_argument("--probe-audit-interval", type=float,
                   default=float(os.environ.get("CSI_PROBE_AUDIT_INTERVAL", "300")),
                   help="seconds between probe audits, 0 to disable (default 300). An audit "
                        "counts the arriving frames for --probe-audit-window seconds with the "
                        "probes running and then for the same window with them stopped, and "
                        "reports the difference as the rate the probes are buying. Whether "
                        "provoked traffic is measurable at all is a property of the access "
                        "point and of the mode, so it is measured rather than assumed")
    p.add_argument("--probe-audit-window", type=float,
                   default=float(os.environ.get("CSI_PROBE_AUDIT_WINDOW", "5")),
                   help="seconds each half of an audit lasts (default 5). The quiet half is a "
                        "gap in supply, not a change of measurement: the sequence counter and "
                        "the link epoch run straight through it and the server sees a lull")
    p.add_argument("--audit-file", default=os.environ.get("CSI_AUDIT_FILE", "/run/csi-node.audit"),
                   help="where to leave the last probe audit for the access-point agent to "
                        "publish (default /run/csi-node.audit). Whether provoking traffic works "
                        "is site-specific, so it belongs in front of an operator rather than "
                        "only in the journal")
    p.add_argument("--rssi-interval", type=float, default=1.0,
                   help="seconds between /proc/net/wireless reads (default 1)")
    p.add_argument("--status-interval", type=float, default=10.0,
                   help="seconds between status lines, 0 to disable (default 10)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if not 1 <= args.node_id <= 254:
        p.error("--node-id must be 1..254; 0 and 255 are reserved")
    if args.probe_audit_interval > 0:
        if args.probe_audit_window <= 0:
            p.error("--probe-audit-window must be positive")
        if 2 * args.probe_audit_window >= args.probe_audit_interval:
            p.error("--probe-audit-interval must leave room for both halves of an audit: "
                    "more than twice --probe-audit-window")
        if args.probe_audit_window >= RELEARN_SECONDS:
            # The audit is only allowed to be a gap in supply. Long enough and it stops being
            # one: with the probes off, whatever kind of frame they were provoking stops, and
            # after RELEARN_SECONDS of that the class lock is given up and relearned -- which
            # bumps the link epoch and tells the server to throw its baseline away.
            p.error(f"--probe-audit-window must be under {RELEARN_SECONDS:g}s, or the frame "
                    "class lock relearns inside the quiet window and bumps the link epoch")
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
