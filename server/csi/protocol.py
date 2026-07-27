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
VERSION = 1

# <  little-endian, no padding
# H  magic      B  version     B  node_id
# I  seq        Q  timestamp
# b  rssi       b  noise_floor B  channel   B  sec_channel
# H  n_sub
_HEADER = struct.Struct("<HBBIQbbBBH")
HEADER_SIZE = _HEADER.size
assert HEADER_SIZE == 22, HEADER_SIZE

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


def encode_frame(frame: Frame) -> bytes:
    """Serialize a Frame back to its uplink datagram.

    Only the recorder's tests and the synthetic generator need this — the server never sends
    uplink frames in production. Keeping it here means encode and decode cannot drift.
    """
    data = np.ascontiguousarray(frame.data, dtype=np.int8)
    if data.size != 2 * frame.n_sub:
        raise ProtocolError(f"data has {data.size} bytes, expected {2 * frame.n_sub}")
    header = _HEADER.pack(
        MAGIC,
        VERSION,
        frame.node_id,
        frame.seq & 0xFFFFFFFF,
        frame.timestamp & 0xFFFFFFFFFFFFFFFF,
        frame.rssi,
        frame.noise_floor,
        frame.channel,
        frame.sec_channel,
        frame.n_sub,
    )
    return header + data.tobytes()


def parse_frame(buf: bytes | bytearray | memoryview, *, received_at: float = 0.0) -> Frame:
    """Parse one uplink datagram. Raises ProtocolError on anything malformed.

    A trailing-bytes datagram is rejected rather than truncated: silently accepting the prefix
    would let a subcarrier-count mismatch masquerade as valid data for hours.
    """
    if len(buf) < HEADER_SIZE:
        raise ProtocolError(f"datagram too short: {len(buf)} < {HEADER_SIZE}")

    (
        magic,
        version,
        node_id,
        seq,
        timestamp,
        rssi,
        noise_floor,
        channel,
        sec_channel,
        n_sub,
    ) = _HEADER.unpack_from(buf, 0)

    if magic != MAGIC:
        raise ProtocolError(f"bad magic 0x{magic:04x}")
    if version != VERSION:
        raise ProtocolError(f"unsupported version {version}")
    if n_sub == 0 or n_sub > MAX_SUBCARRIERS:
        raise ProtocolError(f"implausible n_sub {n_sub}")

    expected = HEADER_SIZE + 2 * n_sub
    if len(buf) != expected:
        raise ProtocolError(f"length {len(buf)} != {expected} implied by n_sub={n_sub}")

    data = np.frombuffer(bytes(buf[HEADER_SIZE:expected]), dtype=np.int8)
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
    )


def peek_n_sub(buf: bytes) -> int:
    """Subcarrier count without building a Frame — used when scanning recordings for an index."""
    if len(buf) < HEADER_SIZE:
        raise ProtocolError("datagram too short")
    return _HEADER.unpack_from(buf, 0)[9]


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
