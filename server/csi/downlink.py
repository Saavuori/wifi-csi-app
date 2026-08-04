"""Server -> browser binary framing. Mirrored in web/src/lib/protocol.ts.

Text WebSocket messages carry JSON events (metrics, node health, session control) at a few Hz.
Binary messages carry CSI frames at the full node rate. Splitting on the WebSocket frame type
means the client dispatches on `typeof ev.data` with no tag byte and no ambiguity, and the
high-rate path never pays for JSON.
"""

from __future__ import annotations

import struct

import numpy as np

from .dsp.preprocess import Processed
from .ring import Window

DOWN_MAGIC = 0x4344
DOWN_VERSION = 1

FLAG_AGC_STEP = 1 << 0
FLAG_REPLAY = 1 << 1

# <  little-endian, no padding
# H magic  B version  B node_id
# I seq    Q timestamp
# b rssi   b noise    B channel  B flags
# H n_sub  H pad
_HEADER = struct.Struct("<HBBIQbbBBHH")
DOWN_HEADER_SIZE = _HEADER.size
assert DOWN_HEADER_SIZE == 24, DOWN_HEADER_SIZE
assert DOWN_HEADER_SIZE % 4 == 0, "amp must land 4-byte aligned for Float32Array in the browser"


def encode_frame(processed: Processed, *, replay: bool = False) -> bytes:
    """One CSI frame for the browser: header plus float32 amplitudes.

    Masked subcarriers are NaN rather than removed. Keeping the array dense means index i is
    always subcarrier i, so the waterfall's Y axis does not shift when the mask changes and the
    client can render the gaps honestly instead of quietly closing them up.
    """
    frame = processed.frame
    flags = 0
    if processed.agc_step:
        flags |= FLAG_AGC_STEP
    if replay:
        flags |= FLAG_REPLAY

    header = _HEADER.pack(
        DOWN_MAGIC,
        DOWN_VERSION,
        frame.node_id,
        frame.seq & 0xFFFFFFFF,
        frame.timestamp & 0xFFFFFFFFFFFFFFFF,
        frame.rssi,
        frame.noise_floor,
        frame.channel,
        flags,
        frame.n_sub,
        0,
    )
    amp = np.ascontiguousarray(processed.amp, dtype="<f4")
    return header + amp.tobytes()


HIST_MAGIC = 0x4348
HIST_VERSION = 1

# <  little-endian, no padding
# H magic  B version  B node_id
# H n_sub  H pad      I count   I pad
_HIST_HEADER = struct.Struct("<HBBHHII")
HIST_HEADER_SIZE = _HIST_HEADER.size
assert HIST_HEADER_SIZE == 16, HIST_HEADER_SIZE
# 16 rather than the 12 the fields need: the timestamps that follow are float64, and both they
# and the float32 amplitudes after them are read as typed-array views in the browser, which
# throws on an offset that is not a multiple of the element size.
assert HIST_HEADER_SIZE % 8 == 0, "timestamps must land 8-byte aligned for Float64Array"


def encode_history(window: Window, node_id: int) -> bytes:
    """A node's recent history as one block, for a browser that has just connected.

    The waterfall is a picture of the last minute or two, and a client that starts from an empty
    canvas has to wait that long before it shows one — during which the view that is meant to be
    the first check on the capture chain says nothing about whether the chain works. The server
    already holds this window for the analyzers, so the fix is to hand it over rather than to
    make each client accumulate its own.

    Same content per column as `encode_frame`, transposed into arrays: the client wants columns
    in bulk here, not a stream of frames it has to reassemble.
    """
    count = len(window)
    header = _HIST_HEADER.pack(
        HIST_MAGIC,
        HIST_VERSION,
        node_id,
        window.n_sub,
        0,
        count,
        0,
    )
    # Device microseconds, as float64 so the browser is not handed a BigInt64Array to unpack.
    # Exact to the microsecond for ~285 years of uptime, which is longer than the counter.
    t_us = np.ascontiguousarray(window.t_us, dtype="<f8")
    amp = np.ascontiguousarray(window.amp, dtype="<f4")
    agc = np.ascontiguousarray(window.agc, dtype=np.uint8)
    return header + t_us.tobytes() + amp.tobytes() + agc.tobytes()


def decode_history(buf: bytes) -> tuple[dict, np.ndarray, np.ndarray, np.ndarray]:
    """Inverse of `encode_history`. Only the tests need this; it keeps the layout honest."""
    if len(buf) < HIST_HEADER_SIZE:
        raise ValueError("short history block")
    magic, version, node_id, n_sub, _pad, count, _pad2 = _HIST_HEADER.unpack_from(buf, 0)
    if magic != HIST_MAGIC or version != HIST_VERSION:
        raise ValueError(f"bad history header 0x{magic:04x} v{version}")

    expected = HIST_HEADER_SIZE + count * (8 + 4 * n_sub + 1)
    if len(buf) != expected:
        raise ValueError(f"length {len(buf)} != {expected}")

    off = HIST_HEADER_SIZE
    t_us = np.frombuffer(buf, dtype="<f8", count=count, offset=off)
    off += 8 * count
    amp = np.frombuffer(buf, dtype="<f4", count=count * n_sub, offset=off).reshape(count, n_sub)
    off += 4 * count * n_sub
    agc = np.frombuffer(buf, dtype=np.uint8, count=count, offset=off)

    meta = {"node_id": node_id, "n_sub": n_sub, "count": count}
    return meta, t_us, amp, agc


def decode_frame(buf: bytes) -> tuple[dict, np.ndarray]:
    """Inverse of `encode_frame`. Only the tests need this; it keeps the layout honest."""
    if len(buf) < DOWN_HEADER_SIZE:
        raise ValueError("short downlink frame")
    (
        magic,
        version,
        node_id,
        seq,
        timestamp,
        rssi,
        noise,
        channel,
        flags,
        n_sub,
        _pad,
    ) = _HEADER.unpack_from(buf, 0)
    if magic != DOWN_MAGIC or version != DOWN_VERSION:
        raise ValueError(f"bad downlink header 0x{magic:04x} v{version}")

    expected = DOWN_HEADER_SIZE + 4 * n_sub
    if len(buf) != expected:
        raise ValueError(f"length {len(buf)} != {expected}")

    amp = np.frombuffer(buf, dtype="<f4", count=n_sub, offset=DOWN_HEADER_SIZE)
    meta = {
        "node_id": node_id,
        "seq": seq,
        "timestamp": timestamp,
        "rssi": rssi,
        "noise_floor": noise,
        "channel": channel,
        "flags": flags,
        "n_sub": n_sub,
        "agc_step": bool(flags & FLAG_AGC_STEP),
        "replay": bool(flags & FLAG_REPLAY),
    }
    return meta, amp
