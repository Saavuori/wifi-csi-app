"""The wire format is pinned here.

Three implementations parse these bytes — Python, C, TypeScript — and only this one is covered
by tests. So the tests assert on literal offsets and literal bytes rather than on round-trips
through the same code that produced them: a round-trip test passes happily while all three
implementations agree on the wrong layout.
"""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest

from csi.downlink import DOWN_HEADER_SIZE, decode_frame
from csi.downlink import encode_frame as encode_down
from csi.dsp.preprocess import Preprocessor
from csi.protocol import (
    HEADER_SIZE,
    MAGIC,
    VERSION,
    Frame,
    ProtocolError,
    encode_frame,
    iter_records,
    parse_frame,
    record_bytes,
)


def make_frame(n_sub: int = 64, **kwargs) -> Frame:
    data = np.arange(2 * n_sub, dtype=np.int16).astype(np.int8)
    defaults = dict(
        node_id=3,
        seq=0xDEADBEEF,
        timestamp=0x0102030405060708,
        rssi=-47,
        noise_floor=-92,
        channel=6,
        sec_channel=0,
        n_sub=n_sub,
        data=data,
    )
    defaults.update(kwargs)
    return Frame(**defaults)


def test_header_is_22_bytes_with_no_padding():
    assert HEADER_SIZE == 22
    assert len(encode_frame(make_frame(64))) == 22 + 128


def test_field_offsets_are_where_the_spec_says():
    raw = encode_frame(make_frame(64))

    assert struct.unpack_from("<H", raw, 0)[0] == MAGIC
    assert raw[0:2] == b"\x53\x43", "magic 0x4353 little-endian is the bytes 53 43"
    assert raw[2] == VERSION
    assert raw[3] == 3
    assert struct.unpack_from("<I", raw, 4)[0] == 0xDEADBEEF
    assert struct.unpack_from("<Q", raw, 8)[0] == 0x0102030405060708
    assert struct.unpack_from("<b", raw, 16)[0] == -47
    assert struct.unpack_from("<b", raw, 17)[0] == -92
    assert raw[18] == 6
    assert raw[19] == 0
    assert struct.unpack_from("<H", raw, 20)[0] == 64


def test_round_trip_preserves_every_field():
    original = make_frame(64)
    parsed = parse_frame(encode_frame(original))

    for field in ("node_id", "seq", "timestamp", "rssi", "noise_floor", "channel", "n_sub"):
        assert getattr(parsed, field) == getattr(original, field)
    np.testing.assert_array_equal(parsed.data, original.data)


def test_interleaving_is_imag_then_real():
    frame = make_frame(2, data=np.array([1, 2, 3, 4], dtype=np.int8))
    np.testing.assert_array_equal(frame.imag, [1, 3])
    np.testing.assert_array_equal(frame.real, [2, 4])
    np.testing.assert_allclose(frame.amplitude(), [np.hypot(1, 2), np.hypot(3, 4)])


@pytest.mark.parametrize("n_sub", [64, 128, 256])
def test_variable_subcarrier_counts(n_sub):
    parsed = parse_frame(encode_frame(make_frame(n_sub)))
    assert parsed.n_sub == n_sub
    assert parsed.data.size == 2 * n_sub


def test_rejects_bad_magic():
    raw = bytearray(encode_frame(make_frame()))
    raw[0] ^= 0xFF
    with pytest.raises(ProtocolError, match="magic"):
        parse_frame(bytes(raw))


def test_rejects_unknown_version():
    raw = bytearray(encode_frame(make_frame()))
    raw[2] = 99
    with pytest.raises(ProtocolError, match="version"):
        parse_frame(bytes(raw))


def test_rejects_truncated_payload():
    raw = encode_frame(make_frame())
    with pytest.raises(ProtocolError, match="length"):
        parse_frame(raw[:-4])


def test_rejects_trailing_bytes():
    """A datagram longer than n_sub implies is a mismatch, not something to truncate. Accepting
    the prefix would let a bandwidth misconfiguration look valid for hours."""
    raw = encode_frame(make_frame()) + b"\x00\x00"
    with pytest.raises(ProtocolError, match="length"):
        parse_frame(raw)


def test_rejects_implausible_subcarrier_count():
    raw = bytearray(encode_frame(make_frame()))
    struct.pack_into("<H", raw, 20, 9999)
    with pytest.raises(ProtocolError, match="n_sub"):
        parse_frame(bytes(raw))


# -- recording container ---------------------------------------------------------------


def test_recording_container_round_trips_datagrams_verbatim():
    datagrams = [encode_frame(make_frame(64, seq=i)) for i in range(5)]
    buf = io.BytesIO(b"CSIREC01" + b"".join(record_bytes(d) for d in datagrams))

    read = [payload for _, payload in iter_records(buf)]
    assert read == datagrams


def test_truncated_recording_yields_the_frames_before_the_tear():
    """A recording cut short by a power loss is a normal thing to have. The frames written
    before the tear are still good data and must still be readable."""
    datagrams = [encode_frame(make_frame(64, seq=i)) for i in range(4)]
    blob = b"CSIREC01" + b"".join(record_bytes(d) for d in datagrams)
    truncated = io.BytesIO(blob[:-30])

    read = [payload for _, payload in iter_records(truncated)]
    assert read == datagrams[:3]


def test_recording_rejects_a_file_that_is_not_a_recording():
    with pytest.raises(ProtocolError, match="not a CSI recording"):
        list(iter_records(io.BytesIO(b"not it at all")))


# -- downlink ---------------------------------------------------------------------------


def test_downlink_amplitudes_are_four_byte_aligned():
    """The browser does `new Float32Array(buf, 24, n_sub)`, which throws if the offset is not
    a multiple of 4. That is the entire reason the header carries a pad."""
    assert DOWN_HEADER_SIZE == 24
    assert DOWN_HEADER_SIZE % 4 == 0


def test_downlink_round_trip():
    processed = Preprocessor().process(make_frame(64))
    meta, amp = decode_frame(encode_down(processed, replay=True))

    assert meta["node_id"] == 3
    assert meta["seq"] == 0xDEADBEEF
    assert meta["n_sub"] == 64
    assert meta["replay"] is True
    assert amp.shape == (64,)
    np.testing.assert_array_equal(np.isnan(amp), ~processed.mask)


def test_downlink_masked_subcarriers_are_nan_not_removed():
    """Index i must always mean subcarrier i, so the waterfall's Y axis does not shift when
    the mask changes."""
    processed = Preprocessor().process(make_frame(64))
    _, amp = decode_frame(encode_down(processed))

    assert amp.size == 64
    assert np.isnan(amp[0]), "DC carries no signal and must be a gap"
    assert not np.isnan(amp[10]), "subcarrier 10 is active"
