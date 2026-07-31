"""Uplink wire format (node -> server) and the recording container.

See docs/wire-format.md. The byte layout here is mirrored in firmware/main/csi_wire.h and
web/src/lib/protocol.ts; test_protocol.py pins it.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

MAGIC = 0x4353
VERSION = 2

# <  little-endian, no padding
# H  magic      B  version     B  node_id
# I  seq        Q  timestamp
# b  rssi       b  noise_floor B  channel   B  sec_channel
# H  n_sub
_HEADER_V1 = struct.Struct("<HBBIQbbBBH")
HEADER_SIZE_V1 = _HEADER_V1.size
assert HEADER_SIZE_V1 == 22, HEADER_SIZE_V1

# v2 appends, leaving every v1 field at the offset it already had:
# 6s src_mac    B  link_epoch  B  reserved
_HEADER_V2 = struct.Struct("<HBBIQbbBBH6sBB")
HEADER_SIZE = _HEADER_V2.size
assert HEADER_SIZE == 30, HEADER_SIZE
assert _HEADER_V2.format.startswith(_HEADER_V1.format), "v2 must extend v1, not rearrange it"

# Both versions are parsed. A node in the field is not upgraded at the same moment the server
# is, and — more importantly — every recording ever made is a stream of datagrams in whatever
# version was current when it was captured. Replay hands those exact bytes to this parser, so
# dropping v1 support would retire the archive.
_HEADERS = {1: _HEADER_V1, 2: _HEADER_V2}

ZERO_MAC = b"\x00" * 6

# A node that reports more than this is either misconfigured or the packet is garbage that
# happened to survive the magic check. 512 leaves room past the 256 a Nexmon node will send.
MAX_SUBCARRIERS = 512

SEC_CHANNEL_NONE = 0
SEC_CHANNEL_ABOVE = 1
SEC_CHANNEL_BELOW = 2


class ProtocolError(ValueError):
    """A datagram could not be parsed. Callers count these; they never crash the listener."""


@dataclass(slots=True)
class Frame:
    """One CSI measurement from one node.

    `data` is the raw int8 buffer as it left the device: 2*n_sub interleaved (imag, real)
    pairs. Amplitude is derived in the DSP layer, not here — this class stays a faithful
    record of the bytes so that a recording round-trips exactly.
    """

    node_id: int
    seq: int
    timestamp: int
    rssi: int
    noise_floor: int
    channel: int
    sec_channel: int
    n_sub: int
    data: np.ndarray  # int8, shape (2 * n_sub,)

    # Wall-clock arrival time, seconds. Used only for node-health bookkeeping and to give
    # recordings a real-world anchor — never for anything frequency-domain, because device
    # timestamps are the only jitter-free clock we have.
    received_at: float = 0.0

    # v2. Who transmitted the frame this measurement came from — the access point's BSSID for a
    # station node, the peer node's MAC for the ESP32 pair. A v1 frame reports six zero bytes.
    src_mac: bytes = ZERO_MAC
    # v2. Increments on the node every time it associates. A change means the link is not the
    # link it was — a roam, a reconnect, the access point changing channel — and every baseline
    # built on the old one is void, so `nodes.py` treats it exactly as it treats a reboot.
    # Always 0 on v1 frames, and a node never mixes versions, so a v1 stream simply never
    # reports a roam.
    link_epoch: int = 0

    @property
    def imag(self) -> np.ndarray:
        return self.data[0::2]

    @property
    def real(self) -> np.ndarray:
        return self.data[1::2]

    def complex(self) -> np.ndarray:
        """Complex CSI as float32, shape (n_sub,)."""
        return self.real.astype(np.float32) + 1j * self.imag.astype(np.float32)

    def amplitude(self) -> np.ndarray:
        """sqrt(re^2 + im^2) per subcarrier, float32, shape (n_sub,).

        Computed in float32 from int8 inputs; the largest possible value is
        sqrt(2)*128 ~= 181, so there is no overflow concern and no need to widen.
        """
        re = self.real.astype(np.float32)
        im = self.imag.astype(np.float32)
        return np.sqrt(re * re + im * im)

    def to_bytes(self) -> bytes:
        return encode_frame(self)


def encode_frame(frame: Frame, *, version: int = VERSION) -> bytes:
    """Serialize a Frame back to its uplink datagram.

    Only the recorder's tests and the synthetic generator need this — the server never sends
    uplink frames in production. Keeping it here means encode and decode cannot drift.

    `version` exists so the tests can build a v1 datagram and prove it still parses.
    """
    if version not in _HEADERS:
        raise ProtocolError(f"cannot encode version {version}")
    data = np.ascontiguousarray(frame.data, dtype=np.int8)
    if data.size != 2 * frame.n_sub:
        raise ProtocolError(f"data has {data.size} bytes, expected {2 * frame.n_sub}")
    fields = [
        MAGIC,
        version,
        frame.node_id,
        frame.seq & 0xFFFFFFFF,
        frame.timestamp & 0xFFFFFFFFFFFFFFFF,
        frame.rssi,
        frame.noise_floor,
        frame.channel,
        frame.sec_channel,
        frame.n_sub,
    ]
    if version >= 2:
        fields += [bytes(frame.src_mac).ljust(6, b"\x00")[:6], frame.link_epoch & 0xFF, 0]
    return _HEADERS[version].pack(*fields) + data.tobytes()


def parse_frame(buf: bytes | bytearray | memoryview, *, received_at: float = 0.0) -> Frame:
    """Parse one uplink datagram. Raises ProtocolError on anything malformed.

    A trailing-bytes datagram is rejected rather than truncated: silently accepting the prefix
    would let a subcarrier-count mismatch masquerade as valid data for hours.
    """
    if len(buf) < HEADER_SIZE_V1:
        raise ProtocolError(f"datagram too short: {len(buf)} < {HEADER_SIZE_V1}")

    magic, version = struct.unpack_from("<HB", buf, 0)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic 0x{magic:04x}")
    header = _HEADERS.get(version)
    if header is None:
        raise ProtocolError(f"unsupported version {version}")
    if len(buf) < header.size:
        raise ProtocolError(f"datagram too short for v{version}: {len(buf)} < {header.size}")

    (
        _magic,
        _version,
        node_id,
        seq,
        timestamp,
        rssi,
        noise_floor,
        channel,
        sec_channel,
        n_sub,
        *extra,
    ) = header.unpack_from(buf, 0)

    if n_sub == 0 or n_sub > MAX_SUBCARRIERS:
        raise ProtocolError(f"implausible n_sub {n_sub}")

    expected = header.size + 2 * n_sub
    if len(buf) != expected:
        raise ProtocolError(f"length {len(buf)} != {expected} implied by n_sub={n_sub}")

    src_mac, link_epoch = (extra[0], extra[1]) if extra else (ZERO_MAC, 0)

    data = np.frombuffer(bytes(buf[header.size : expected]), dtype=np.int8)
    return Frame(
        node_id=node_id,
        seq=seq,
        timestamp=timestamp,
        rssi=rssi,
        noise_floor=noise_floor,
        channel=channel,
        sec_channel=sec_channel,
        n_sub=n_sub,
        data=data,
        received_at=received_at,
        src_mac=src_mac,
        link_epoch=link_epoch,
    )


def peek_n_sub(buf: bytes) -> int:
    """Subcarrier count without building a Frame — used when scanning recordings for an index.

    Version-independent: `n_sub` sits at the same offset in both, which is the point of having
    appended to v1 rather than rearranged it.
    """
    if len(buf) < HEADER_SIZE_V1:
        raise ProtocolError("datagram too short")
    return _HEADER_V1.unpack_from(buf, 0)[9]


# --------------------------------------------------------------------------------------
# Recording container
# --------------------------------------------------------------------------------------

REC_MAGIC = b"CSIREC01"
INDEX_STRIDE = 256
INDEX_ENTRY = struct.Struct("<QQ")  # timestamp_us, byte_offset
_LEN = struct.Struct("<H")


def iter_records(fp) -> Iterator[tuple[int, bytes]]:
    """Yield (byte_offset, datagram) from an open recording file.

    Stops cleanly at the first short read. A recording truncated by a power cut is a normal
    thing to have, not an error — the frames before the tear are still perfectly good.
    """
    header = fp.read(len(REC_MAGIC))
    if header != REC_MAGIC:
        raise ProtocolError(f"not a CSI recording (magic {header!r})")

    offset = len(REC_MAGIC)
    while True:
        raw_len = fp.read(_LEN.size)
        if len(raw_len) < _LEN.size:
            return
        (length,) = _LEN.unpack(raw_len)
        payload = fp.read(length)
        if len(payload) < length:
            return
        yield offset, payload
        offset += _LEN.size + length


def record_bytes(datagram: bytes) -> bytes:
    """Length-prefix one datagram for storage."""
    if len(datagram) > 0xFFFF:
        raise ProtocolError(f"datagram of {len(datagram)} bytes exceeds the u16 length prefix")
    return _LEN.pack(len(datagram)) + datagram
