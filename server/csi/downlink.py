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
