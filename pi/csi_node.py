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
  rssi       upstream nexmon_csi carries no RSSI, so we read the driver's own estimate from
             /proc/net/wireless. It is smoothed and slow, which happens to be exactly what the
             server wants — its hybrid normalization only uses RSSI through a 30-second EMA (see
             server/csi/dsp/preprocess.py). It does assume an association: a monitor radio has no
             link for the driver to describe, and this is the first field to distrust there. The
             7_45_189 build's 18-byte nexmon header carries a per-frame RSSI that supersedes it,
             which parse_nexmon does not read yet.
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
import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

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
_OFF_SRC_MAC = 4
_OFF_SEQ = 10
_OFF_CORE_SPATIAL = 12
_OFF_CHANSPEC = 14

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


# Widths a capture can legitimately have. Anything one sample longer is the firmware quirk
# handled by `_trim_overlong`.
_VALID_WIDTHS = (64, 128, 256)


def _trim_overlong(csi: np.ndarray) -> np.ndarray:
    """Drop the trailing sample from a frame that is one longer than a real width.

    About 1% of frames from this bcm43455 build arrive as 1046 bytes rather than 1042 — 257
    subcarriers instead of 256. The extra sample is at the end, not the start: correlating the
    mean magnitude profile of the odd frames against the 256-wide population gives 0.997 when
    the last sample is dropped and 0.236 when the first is, so the leading 256 are the real
    measurement and the tail is junk.

    Worth handling rather than dropping the frame, because the width is part of the link key.
    Left alone, every one of these flipped the key 256 -> 257 -> 256 and bumped the link epoch
    twice, and the server answers an epoch change by discarding that node's history. At 1% of
    41 Hz that was a reset every couple of seconds, which is why presence and breathing never
    accumulated a usable window while the waterfall looked perfectly healthy.
    """
    if csi.size in _VALID_WIDTHS:
        return csi
    if csi.size - 1 in _VALID_WIDTHS:
        return csi[:-1]
    return csi


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
    csi = _trim_overlong(csi)

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

# Active subcarrier ranges, |k| inclusive, mirroring LAYOUTS in server/csi/dsp/subcarriers.py.
# Everything outside them is DC or guard band: never transmitted, so the FFT bin holds whatever
# was left in it. On a bcm43455 that residue is not small — measured against live captures, the
# guard bins run to 34640 while the largest real data subcarrier in the same frames was 2074.
#
# Any statistic taken over a whole frame is therefore a statistic about the guard bands. Both
# callers below learned this the hard way: `quantize` was scaling every frame to a DC bin
# seventeen times larger than the signal, which quantized the actual measurement to zero, and
# `occupied_span` was comparing block powers dominated by the same residue and so picked the
# wrong block and failed the ratio test.
_ACTIVE: dict[int, tuple[int, int]] = {64: (1, 26), 128: (2, 58), 256: (2, 122)}


def data_bins(n_sub: int) -> np.ndarray:
    """Boolean mask of the bins that carry data or pilots, in this file's index convention.

    Index i maps to subcarrier k as k = i for i < n_sub/2, else i - n_sub — the layout the
    server assumes and the one nexmon delivers, so index 0 is DC.
    """
    i = np.arange(n_sub)
    k = np.where(i < n_sub // 2, i, i - n_sub)
    lo, hi = _ACTIVE.get(n_sub, (1, max(1, n_sub // 2 - max(1, n_sub // 16))))
    return (np.abs(k) >= lo) & (np.abs(k) <= hi)


def _masked_mean_power(block: np.ndarray, mask: np.ndarray) -> float:
    """Mean power of one block over its data bins, or 0.0 if the block is all guard."""
    kept = block[mask]
    if kept.size == 0:
        return 0.0
    return float((np.abs(kept) ** 2).mean())


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

    # Guards excluded before the blocks are averaged. With them in, a live capture measured
    # [1.9e7, 2.4e6, 5.9e6, 2.1e5] and named Q0 the loudest at 4.7 dB — below the margin, so
    # nothing was ever trimmed. The same frames with only data bins counted are
    # [1.3e5, 6.7e4, 2.3e6, 2.1e5]: Q2, by 10.4 dB. The old reading was not marginal, it was
    # the wrong block entirely.
    mask = data_bins(csi.size)
    power = np.array(
        [
            _masked_mean_power(csi[b * NARROWBAND_BLOCK : (b + 1) * NARROWBAND_BLOCK],
                               mask[b * NARROWBAND_BLOCK : (b + 1) * NARROWBAND_BLOCK])
            for b in range(blocks)
        ]
    )
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

    Scaled from the *data* subcarriers only. Scaling to the frame maximum sounds equivalent and
    is not: DC and the guard bands are never transmitted, and on this chip the residue sitting in
    those bins runs an order of magnitude above the real signal. Measured over live frames, the
    largest guard bin was 34640 against a largest data bin of 2074, so the scale came out ~17x
    too small and the median data subcarrier quantized to 0.5 — i.e. to zero. Three quarters of
    every frame reached the server as exact zeros, which is what the waterfall was drawing.
    Excluding the guards moves that median to 18.8: same int8, thirty-seven times the resolution.

    Guard bins still travel, they are simply no longer allowed to set the scale — they clip,
    and the server masks them out anyway.
    """
    mag = np.abs(csi)
    if csi.size:
        mask = data_bins(csi.size)
        kept = mag[mask]
        peak = float(kept.max()) if kept.size else float(mag.max())
    else:
        peak = 0.0
    scale = QUANT_PEAK / peak if peak > 0.0 else 0.0

    # Clipped, not wrapped. Without this the guard bins — which now exceed full scale by design
    # — would overflow int8 and come out as small negative numbers: noise dressed up as signal,
    # in the bins a reader is least likely to check.
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

    Superseded, and kept only because the associated-mode path around it has not been removed
    yet. The premise was that an associated chip hands up CSI for frames addressed to this Pi,
    so provoking replies would set the rate — the same trick CSI_PROBE_UDP_ECHO plays in the
    station firmware. Measured on hardware, those replies are precisely what never arrives: the
    extractor's ucode deafens the PHY across every CSI dump and unicast data dies there, so an
    associated Pi sees beacons and broadcast and nothing else. See pi/README.md.

    MulticastStimulus is the working answer, and it works because it never needs a frame
    addressed to this Pi at all.
    """

    def __init__(self, target: str, hz: float, iface: str | None = None) -> None:
        super().__init__(name="prober", daemon=True)
        self.target = target
        self.iface = iface
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


class ClassFollower:
    """Picks the frame class a *passive* node should measure, from what is actually arriving.

    `RateGate` answers a different question — whether to switch the stimulus on — and it owns
    the class whenever the stimulus is running, because then the node knows what it is causing.
    With the stimulus off, nothing does, and the class was simply stuck at full width. On an
    access point whose traffic is all legacy 20 MHz (beacons, and anything to a broadcast
    address) that is the wrong half of the world: every frame is detected as narrowband,
    dropped as off-class, and the node reports a healthy service capturing nothing. Measured on
    hardware, that took a working 41 Hz down to 1.7 Hz.

    So: follow the majority. Same hysteresis argument as RateGate — a class change costs the
    server the node's history, so a decision has to hold for `dwell_s` before another can be
    made, and the majority has to be a real one rather than a coin flip.
    """

    def __init__(self, *, window_s: float = 5.0, dwell_s: float = 30.0, margin: float = 2.0):
        self.window_s = window_s
        self.dwell_s = dwell_s
        # How many times more of one class than the other before it counts as the answer.
        self.margin = margin

        self.changes = 0
        self._window_end: float | None = None
        self._next_change = 0.0
        self._seen = (0, 0)

    def update(self, now: float, narrow_seen: int, wide_seen: int, current: bool) -> bool:
        """Called on every wakeup with the node's running counts. Returns the class to be in."""
        if self._window_end is None:
            self._window_end = now + self.window_s
            self._next_change = now + self.dwell_s
            self._seen = (narrow_seen, wide_seen)
            return current
        if now < self._window_end:
            return current

        narrow = narrow_seen - self._seen[0]
        wide = wide_seen - self._seen[1]
        self._seen = (narrow_seen, wide_seen)
        self._window_end = now + self.window_s

        if now < self._next_change or (narrow + wide) == 0:
            return current

        if narrow > wide * self.margin:
            want = True
        elif wide > narrow * self.margin:
            want = False
        else:
            return current

        if want == current:
            return current
        self.changes += 1
        self._next_change = now + self.dwell_s
        return want


# -- server control -----------------------------------------------------------------------

# Matches the server's validation in server/csi/nodecontrol.py. Anything else in a desired
# config is left alone rather than guessed at: the server is supposed to have validated it, but
# the node is the one holding the radio, and a bad spec applied here breaks the capture.
_CHANNEL_SPEC_RE = re.compile(r"^\d{1,3}/(20|40|80)$")
_STIMULUS_MODES = ("auto", "always", "off")


def freq_to_channel(freq_mhz: int) -> int:
    """IEEE channel number from a centre frequency, the same arithmetic iw itself uses."""
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz < 2484:
        return (freq_mhz - 2407) // 5
    if 5955 <= freq_mhz <= 7115:  # 6 GHz: channel 1 starts at 5955
        return (freq_mhz - 5950) // 5
    if 4910 <= freq_mhz <= 5895:
        return (freq_mhz - 5000) // 5
    return 0


def parse_iw_scan(text: str) -> list[dict]:
    """`iw dev <iface> scan` output into one dict per BSS.

    Text parsing of a tool that warns its output is not a stable API — kept deliberately
    minimal (fields that have printed the same way for a decade) and forgiving: a BSS whose
    block this cannot read becomes an entry with what was readable, never an exception. The
    scan page rendering "width 20" for an AP whose width was unreadable beats a node that
    stops reporting because someone's beacon had an exotic element in it.

    Station count and channel utilisation come from the BSS Load element (802.11e), which most
    consumer APs include: utilisation is the fraction of time the AP sensed the channel busy,
    normalized to 0..1 here from the spec's 0..255.
    """
    aps: list[dict] = []
    ap: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if raw_line.startswith("BSS "):
            match = re.match(r"BSS ([0-9a-f:]{17})", raw_line, re.IGNORECASE)
            ap = None
            if match is not None:
                ap = {
                    "bssid": match.group(1).lower(),
                    "ssid": "",
                    "freq": 0,
                    "channel": 0,
                    "width": 20,
                    "signal": None,
                    "stations": None,
                    "utilisation": None,
                    "associated": "-- associated" in raw_line,
                }
                aps.append(ap)
            continue
        if ap is None:
            continue
        if line.startswith("freq:"):
            try:
                ap["freq"] = int(float(line.split(":", 1)[1].strip()))
            except ValueError:
                continue
            ap["channel"] = freq_to_channel(ap["freq"])
        elif line.startswith("signal:"):
            match = re.search(r"(-?\d+(?:\.\d+)?)", line)
            if match is not None:
                ap["signal"] = float(match.group(1))
        elif line.startswith("SSID:"):
            ap["ssid"] = line.split(":", 1)[1].strip()
        elif line.startswith("* primary channel:"):
            try:
                ap["channel"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("* secondary channel offset:"):
            if line.split(":", 1)[1].strip() in ("above", "below") and ap["width"] < 40:
                ap["width"] = 40
        elif line.startswith("* channel width:"):
            # VHT operation: "1 (80 MHz)" or "2 (160 MHz)"; HE prints similarly.
            match = re.search(r"\((\d+) MHz\)", line)
            if match is not None:
                ap["width"] = max(ap["width"], int(match.group(1)))
        elif line.startswith("* station count:"):
            try:
                ap["stations"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("* channel utilisation:"):
            match = re.search(r"(\d+)/255", line)
            if match is not None:
                ap["utilisation"] = round(int(match.group(1)) / 255, 3)
    aps.sort(key=lambda entry: entry["signal"] if entry["signal"] is not None else -1000,
             reverse=True)
    return aps


class ControlState:
    """What the control loop has decided, read by the capture loop.

    Attribute reads and writes of a str are atomic under the GIL, and the capture loop
    re-reads it every wakeup, so no lock is needed — the worst case is one wakeup of lag.
    """

    def __init__(self, stimulus_mode: str) -> None:
        self.stimulus_mode = stimulus_mode


def _run_command(argv: list[str], timeout_s: float = 30.0) -> str:
    """Run a helper, return its stdout. Raises CalledProcessError with stderr attached."""
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_s, check=True
    )
    return result.stdout


class Controller(threading.Thread):
    """Polls the server for this node's desired configuration and applies it.

    The uplink is one-way UDP, so control rides the other transport the node already has a
    route for: plain HTTP to the same server. Every poll ends with a report of what is
    actually applied — the server treats absence of reports as "this node does not take
    control", so a node behind an old server, or a server behind an old node, degrades to
    exactly the behaviour both had before this existed.

    Three desired fields are understood:
      channel   "auto" follows the association (capture-up), "CH/BW" retunes the extractor
                to an explicit channel, unfiltered.
      stimulus  handed to the capture loop through `ControlState`; applied within one wakeup.
      scan_rev  when it moves, stop collection, `iw scan`, retune, and report what was seen.
                Capture is dark for the few seconds the scan takes; the epoch mechanics treat
                the retune as the link change it is.
    """

    def __init__(
        self,
        url: str,
        node_id: int,
        state: ControlState,
        node: Node,
        iface: str,
        *,
        poll_s: float = 3.0,
        runner=_run_command,
    ) -> None:
        super().__init__(name="controller", daemon=True)
        self.url = url.rstrip("/")
        self.node_id = node_id
        self.state = state
        self.node = node
        self.iface = iface
        self.poll_s = poll_s
        self.runner = runner

        # What this thread believes it has applied. Channel starts as "auto" because the
        # service's ExecStartPre (capture-up.sh) has already followed the association by the
        # time this thread first polls.
        self.applied_channel = "auto"
        self.applied_rev: int | None = None
        self.applied_scan_rev: int | None = None
        self.retunes = 0
        self.scans = 0
        self.failures = 0

        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("control: polling %s every %gs", self.url, self.poll_s)
        while not self._stop.is_set():
            try:
                self._poll_once()
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.failures += 1
                if self.failures in (1, 10) or self.failures % 100 == 0:
                    log.warning("control poll failed (%d so far): %s", self.failures, exc)
            self._stop.wait(self.poll_s)

    # -- one round ------------------------------------------------------------------------

    def _poll_once(self) -> None:
        control = self._http("GET", f"/api/nodes/{self.node_id}/control")
        desired = control.get("desired") or {}
        revision = int(control.get("revision", 0))
        scan_rev = int(control.get("scan_rev", 0))

        if self.applied_rev != revision:
            self._apply(desired)
            self.applied_rev = revision

        scan = None
        if self.applied_scan_rev is None:
            # First contact: whatever scans were requested before this process existed are
            # not owed retroactively.
            self.applied_scan_rev = scan_rev
        elif scan_rev > self.applied_scan_rev:
            scan = self._scan()
            self.applied_scan_rev = scan_rev

        self._report(scan)

    def _apply(self, desired: dict) -> None:
        mode = desired.get("stimulus")
        if mode in _STIMULUS_MODES and mode != self.state.stimulus_mode:
            log.info("control: stimulus mode -> %s", mode)
            self.state.stimulus_mode = mode

        channel = desired.get("channel")
        if isinstance(channel, str) and channel != self.applied_channel:
            if channel == "auto" or _CHANNEL_SPEC_RE.match(channel):
                self._retune(channel)

    def _retune(self, channel: str) -> None:
        if channel == "auto":
            script = os.environ.get("CSI_CAPTURE_UP_SH", "")
            argv = [script]
        else:
            script = os.environ.get("CSI_TUNE_SH", "")
            argv = [script, channel]
        if not script:
            log.warning("control: no retune helper configured; cannot apply channel %r "
                        "(set CSI_TUNE_SH / CSI_CAPTURE_UP_SH in /etc/default/csi-node)",
                        channel)
            return
        log.info("control: retuning to %s", channel)
        self.runner(argv)
        self.applied_channel = channel
        self.retunes += 1

    def _scan(self) -> dict | None:
        """Stop collection, scan, retune, and hand back what was heard.

        The retune runs in a `finally`: a scan that failed halfway must not leave the radio
        in managed mode with the service still believing it is capturing.
        """
        iw = os.environ.get("CSI_IW") or shutil.which("iw") or "/usr/sbin/iw"
        connect = os.environ.get("CSI_CONNECT_SH", "")
        log.info("control: scanning on %s (capture pauses)", self.iface)
        self.scans += 1
        try:
            if connect:
                self.runner([connect, "-i", self.iface, "--stop"])
            out = None
            for attempt in range(3):
                try:
                    out = self.runner([iw, "dev", self.iface, "scan"], timeout_s=20.0)
                    break
                except subprocess.CalledProcessError:
                    # -EBUSY while wpa_supplicant reclaims the interface after --stop.
                    if attempt == 2:
                        raise
                    time.sleep(2.0)
            return {"aps": parse_iw_scan(out or "")}
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("control: scan failed: %s", exc)
            return None
        finally:
            try:
                self._retune(self.applied_channel)
            except (subprocess.SubprocessError, OSError) as exc:
                log.error("control: could not restore capture after scan: %s", exc)

    def _report(self, scan: dict | None) -> None:
        payload: dict = {
            "revision": self.applied_rev or 0,
            "scan_rev": self.applied_scan_rev or 0,
            "applied": {
                "channel": self.applied_channel,
                "stimulus": self.state.stimulus_mode,
                "narrowband": self.node.narrowband,
                "observed_channel": self.node.current_channel(),
                "capabilities": ["channel", "stimulus", "scan"],
            },
        }
        if scan is not None:
            payload["scan"] = scan
        self._http("POST", f"/api/nodes/{self.node_id}/control/report", payload)

    def _http(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            body = json.loads(response.read().decode())
        return body if isinstance(body, dict) else {}


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
        # The same for narrowband. ClassFollower compares the two to decide what this node
        # should be measuring when it is not the one generating the traffic.
        self.narrowband_seen = 0

        # Which class of frame this node is currently forwarding. False is full width — real
        # traffic. True is the 20 MHz world the Ethernet stimulus lives in. The capture loop
        # flips it; see RateGate.
        self.narrowband = False

        self._link: tuple[bytes, int, int] | None = None
        self._t0_us: int | None = None
        # The channel of the last frame forwarded, so the controller can report what the radio
        # is actually on rather than only what it was asked for. 0 until the first frame.
        self._channel = 0

    def current_channel(self) -> int:
        return self._channel

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
        elif span is not None and splittable:
            self.narrowband_seen += 1
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
            # measurement came from. In monitor mode that is whoever was talking, not
            # necessarily the access point, which is why the CSI_AP_MAC filter exists.
            src_mac=pkt.src_mac,
            link_epoch=self.epoch,
        )
        self._channel = chanspec_channel(pkt.chanspec)
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
            log.info("probing %s at %g Hz via %s", target, args.probe_hz, args.iface)
            prober = Prober(target, args.probe_hz, iface=args.iface)
            prober.start()

    # The stimulus mode lives here rather than in `args` so the controller can change it at
    # runtime. The capture loop reads `control.stimulus_mode` every wakeup and asks for the
    # arm state that mode implies, which is what lets the UI's "listen only / generate traffic"
    # switch take effect on a running node.
    control = ControlState(args.stimulus)

    # Built whenever a rate is configured, even when the initial mode is "off": a node the UI
    # can later switch to "always" needs the thread and its socket to already exist. It stays
    # disarmed and silent until something asks for it, so an off node costs one idle thread.
    stimulus = None
    gate = None
    if args.stimulus_hz > 0:
        stimulus = MulticastStimulus(
            args.stimulus_iface, args.stimulus_group, args.stimulus_port, args.stimulus_hz
        )
        stimulus.start()
        gate = RateGate(
            floor_hz=args.stimulus_floor_hz,
            ceiling_hz=args.stimulus_ceiling_hz,
            window_s=args.stimulus_window,
            dwell_s=args.stimulus_dwell,
        )
        log.info(
            "stimulus: %s -> %s:%d at %g Hz, mode %s (auto arms below %g Hz of real traffic)",
            args.stimulus_iface, args.stimulus_group, args.stimulus_port,
            args.stimulus_hz, args.stimulus, args.stimulus_floor_hz,
        )

    log.info(
        "node %d: %s -> %s:%d, core %d stream %d",
        args.node_id, args.iface, args.server, args.port, args.core, args.spatial,
    )

    controller = None
    if args.control_url:
        controller = Controller(
            args.control_url, args.node_id, control, node, args.iface,
            poll_s=args.control_poll,
        )
        controller.start()

    # Tracks what the stimulus thread has been told, so an arm/disarm is logged once rather
    # than on every wakeup. Previously this was inferred from node.narrowband, which now moves
    # for a second reason and would have desynchronised the two.
    stimulus_armed = False
    follower = ClassFollower(
        window_s=args.stimulus_window, dwell_s=args.stimulus_dwell
    )

    bad = 0
    unstamped = 0
    n_sub = 0
    reported = time.monotonic()

    def due_to_report(now: float) -> bool:
        return args.status_interval > 0 and now - reported >= args.status_interval

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
                    if bad == 1:
                        # The status line below carries the running count; this carries the
                        # shape, once, because it is identical for every packet and it is the
                        # number that names the fault. A bcm43455c0 at 80 MHz emits 1042 bytes,
                        # and a header two bytes short leaves a body that is not a whole number
                        # of int16 pairs — so every real frame is rejected, and the only visible
                        # symptom is a node that looks like a dead radio.
                        log.warning(
                            "malformed packet: %d bytes, header expects %d. Is this nexmon_csi "
                            "output?", len(payload), NEXMON_HEADER_SIZE,
                        )
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

            mode = control.stimulus_mode
            if stimulus is not None:
                # The mode governs the arm state; the gate only gets a vote in "auto". Reading
                # the mode here rather than at startup is what lets the UI switch a running node
                # between listening and generating traffic. The gate is still stepped in every
                # mode so its rate estimate stays warm for the moment auto is chosen again.
                gate_armed = gate.update(now, node.wideband_seen) if gate is not None else False
                armed = mode == "always" or (mode == "auto" and gate_armed)
                if armed != stimulus_armed:
                    stimulus_armed = armed
                    stimulus.arm(armed)
                    log.info(
                        "%s the stimulus (%s)",
                        "arming" if armed else "disarming",
                        f"{gate.rate:.1f} Hz real traffic" if mode == "auto" and gate else mode,
                    )
            else:
                armed = False

            # While the stimulus is running the node knows what it is causing, so it measures
            # that. Otherwise it has no traffic of its own and should measure whatever the air
            # actually carries — see ClassFollower.
            want = True if armed else follower.update(
                now, node.narrowband_seen, node.wideband_seen, node.narrowband
            )
            if want != node.narrowband:
                node.narrowband = want
                log.info(
                    "measuring %s from here (%s)",
                    "20 MHz" if want else "full width",
                    "stimulus armed" if armed else "following the traffic on air",
                )

            if due_to_report(now):
                rate = node.frames / max(now - reported, 1e-9)
                extra = ""
                if stimulus is not None:
                    # From what the stimulus thread was actually told, not from the frame class.
                    # Those used to be the same fact; since a passive node can select 20 MHz on
                    # its own, reading the class here reported "stimulus on" for a node that had
                    # sent nothing — the status line contradicting the (0 sent) beside it.
                    state = "on" if stimulus_armed else "off"
                    extra = f", stimulus {state} ({stimulus.sent} sent)"
                log.info(
                    "%d frames (%.1f Hz), %d subcarriers, rssi %d dBm, %d link changes, "
                    "%d malformed, %d unstamped, %d off-class%s",
                    node.frames, rate, n_sub, node.rssi, node.link_changes, bad, unstamped,
                    node.dropped_class, extra,
                )
                if (
                    stimulus is not None
                    and stimulus_armed
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
        if controller is not None:
            controller.stop()
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
    p.add_argument("--control-url",
                   default=os.environ.get("CSI_CONTROL_URL") or None,
                   help="base URL of the server's HTTP API to poll for channel/stimulus "
                        "control (e.g. http://127.0.0.1:8080); disabled if unset")
    p.add_argument("--control-poll", type=float,
                   default=float(os.environ.get("CSI_CONTROL_POLL", "3")),
                   help="seconds between control polls (default 3)")
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
