"""Tests for the Raspberry Pi node.

The load-bearing ones feed synthetic nexmon packets through the node and parse the result with
the *server's* own protocol module. That is what stops the fourth copy of the wire header from
drifting away from the other three, and it means the translation can be exercised without a Pi.
"""

from __future__ import annotations

import logging
import struct

import csi_node
import numpy as np
import pytest

# The server is a dependency of the test run, not of the node itself.
from csi import protocol
from csi.nodes import NodeHealth
from csi.protocol import parse_frame
from csi_node import (
    NEXMON_HEADER_SIZE,
    Node,
    ProbeAudit,
    chanspec_channel,
    chanspec_sec_channel,
    encode_uplink,
    parse_mac,
    parse_nexmon,
    quantize,
    read_wireless,
    timestamp_us,
    udp_payload,
)

AP_MAC = bytes.fromhex("001122334455")
# 2.4 GHz channel 6, 20 MHz.
CHANSPEC_CH6 = 0x1006

# The two 802.11 frame-control bytes this link actually carries, measured over a capture:
# 98.2% block acks against 1.8% QoS data. They are not interchangeable samples — their medians
# correlate only 0.918 with each other while each self-correlates above 0.99 — which is the
# whole reason Node locks onto one class.
BLOCK_ACK = 0x94
QOS_DATA = 0x88


def nexmon_packet(
    csi: np.ndarray,
    *,
    src_mac: bytes = AP_MAC,
    seq: int = 0,
    core: int = 0,
    spatial: int = 0,
    chanspec: int = CHANSPEC_CH6,
    chanspec_big_endian: bool = False,
    rssi: int = 0,
    fctl: int = BLOCK_ACK,
) -> bytes:
    """Build a nexmon CSI payload. `csi` is complex; real and imag are stored int16.

    Eighteen bytes of header, not the sixteen this file used to build: magic u16, rssi i8,
    frame_control u8, src_mac, seq u16, core/spatial u16, chanspec u16, chip u16. Getting this
    wrong was not a cosmetic error — it is what made the node reject every real frame. A
    bcm43455c0 emits 1042 bytes at 80 MHz, and (1042 - 16) % 4 == 2 fails parse_nexmon's "an
    int16 pair per subcarrier" check, so every packet came back None and the loop counted it
    without saying so. The tests passed throughout, because they built the same wrong header.
    """
    chan = struct.pack(">H" if chanspec_big_endian else "<H", chanspec)
    header = (
        struct.pack("<H", csi_node.NEXMON_MAGIC)
        + struct.pack("<bB", rssi, fctl)
        + src_mac
        + struct.pack("<H", seq)
        + struct.pack("<H", (spatial << 3) | core)
        + chan
        + struct.pack("<H", 0x02BC)
    )
    assert len(header) == NEXMON_HEADER_SIZE
    body = np.empty(2 * csi.size, dtype="<i2")
    body[0::2] = np.rint(csi.real)
    body[1::2] = np.rint(csi.imag)
    return header + body.tobytes()


def sample_csi(n_sub: int = 64, scale: float = 800.0) -> np.ndarray:
    k = np.arange(n_sub)
    return scale * (np.cos(k / 5.0) + 1j * np.sin(k / 7.0))


# How long the real learning window runs, and how many frames close it early. Named so the
# tests that drive it read against the defaults rather than against magic numbers.
LEARN_FRAMES = 400
LEARN_SECONDS = 2.0
LEARN_MIN = 50
LEARN_MAX_SECONDS = 30.0
RELEARN_SECONDS = 20.0


def locked_node(**kw) -> Node:
    """A Node whose class lock closes on its first frame.

    Nothing is emitted while the node is still deciding which (frame class, subcarrier count)
    it is measuring — a baseline built from a mixture of frame kinds describes no channel — and
    on the air that decision takes a couple of seconds or a few hundred frames. A test that
    feeds ten packets in a tight loop closes neither window and gets None back every time.

    So the tests about everything *else* build the node with a one-frame window. The same
    learn-then-lock path runs, it just finishes immediately. How the window itself behaves —
    how long it runs, what it picks out of a mixture, when it gives a lock up — is driven
    against the real defaults further down, with the clock injected.
    """
    kw.setdefault("learn_frames", 1)
    kw.setdefault("learn_seconds", 0.0)
    return Node(**kw)


# -- nexmon decoding ---------------------------------------------------------------------


def test_parses_a_well_formed_packet():
    csi = sample_csi()
    pkt = parse_nexmon(nexmon_packet(csi, core=1, spatial=2, rssi=-47, fctl=BLOCK_ACK))

    assert pkt is not None
    assert pkt.n_sub == 64
    assert pkt.src_mac == AP_MAC
    assert (pkt.core, pkt.spatial) == (1, 2)
    assert chanspec_channel(pkt.chanspec) == 6
    assert pkt.rssi == -47
    assert pkt.fctl == BLOCK_ACK
    np.testing.assert_allclose(pkt.csi, np.rint(csi.real) + 1j * np.rint(csi.imag))


def test_the_header_is_eighteen_bytes_and_an_80mhz_frame_decodes():
    """The regression this file was built to miss.

    1042 bytes is what a bcm43455c0 puts on the wire at 80 MHz. Read against a 16-byte header
    that leaves 1026 payload bytes, which is not a whole number of (int16, int16) pairs, so
    parse_nexmon returned None for every frame — and the capture loop's malformed branch
    `continue`d past the status line, so it did it in complete silence.
    """
    assert NEXMON_HEADER_SIZE == 18
    payload = nexmon_packet(sample_csi(256))
    assert len(payload) == 1042
    pkt = parse_nexmon(payload)
    assert pkt is not None and pkt.n_sub == 256


def test_rssi_is_signed():
    """It is a dBm in one byte, so every value that matters is negative."""
    pkt = parse_nexmon(nexmon_packet(sample_csi(), rssi=-90))
    assert pkt is not None and pkt.rssi == -90


@pytest.mark.parametrize("n_sub", [64, 128, 256])
def test_subcarrier_count_comes_from_the_length(n_sub):
    """n_sub is never assumed. 256 is the 80 MHz case this hardware exists to reach."""
    pkt = parse_nexmon(nexmon_packet(sample_csi(n_sub)))
    assert pkt is not None and pkt.n_sub == n_sub


# The magic is the first two bytes *of* the header, so a header-sized payload is
# NEXMON_HEADER_SIZE bytes in total and anything past that is CSI.
_MAGIC = struct.pack("<H", 0x1111)
_REST_OF_HEADER = bytes(NEXMON_HEADER_SIZE - 2)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x11\x11",
        struct.pack("<H", 0x2222) + _REST_OF_HEADER + bytes(4),  # wrong magic
        _MAGIC + _REST_OF_HEADER,  # header only, no CSI
        _MAGIC + _REST_OF_HEADER + bytes(3),  # not a whole subcarrier
        _MAGIC + _REST_OF_HEADER + bytes(6),  # one and a half subcarriers
    ],
)
def test_malformed_packets_return_none_rather_than_raising(payload):
    assert parse_nexmon(payload) is None


def test_a_single_subcarrier_is_the_smallest_valid_packet():
    """Guards the boundary the malformed cases sit against."""
    pkt = parse_nexmon(_MAGIC + _REST_OF_HEADER + bytes(4))
    assert pkt is not None and pkt.n_sub == 1


def test_chanspec_endianness_is_resolved_by_plausibility():
    """Firmware builds have differed here, and guessing wrong mislabels every frame."""
    little = parse_nexmon(nexmon_packet(sample_csi(), chanspec=CHANSPEC_CH6))
    big = parse_nexmon(
        nexmon_packet(sample_csi(), chanspec=CHANSPEC_CH6, chanspec_big_endian=True)
    )
    assert little is not None and big is not None
    assert chanspec_channel(little.chanspec) == 6
    assert chanspec_channel(big.chanspec) == 6


def test_sec_channel_is_none_at_20mhz_and_set_above_it():
    assert chanspec_sec_channel(0x1006) == csi_node.SEC_CHANNEL_NONE
    assert chanspec_sec_channel(0x1806) == csi_node.SEC_CHANNEL_ABOVE
    assert chanspec_sec_channel(0x1906) == csi_node.SEC_CHANNEL_BELOW


# -- quantization ------------------------------------------------------------------------


def test_quantize_swaps_to_the_wire_order():
    """nexmon interleaves (real, imag); the uplink wire is (imag, real)."""
    csi = np.array([100.0 + 50.0j], dtype=np.complex64)
    out = quantize(csi)
    # Peak magnitude is |100+50j| ~= 111.8, scaled so that lands at 120.
    scale = csi_node.QUANT_PEAK / abs(complex(100.0, 50.0))
    assert out[0] == round(50.0 * scale)
    assert out[1] == round(100.0 * scale)


def test_quantize_never_clips():
    for scale in (1.0, 100.0, 32000.0):
        out = quantize(sample_csi(64, scale))
        assert out.dtype == np.int8
        assert np.abs(out).max() <= 127


def test_quantize_handles_an_all_zero_frame():
    out = quantize(np.zeros(64, dtype=np.complex64))
    assert out.size == 128 and not out.any()


def test_quantize_preserves_relative_structure():
    """Absolute gain is discarded; the shape across subcarriers is what carries the signal."""
    csi = sample_csi(64)
    a = quantize(csi).astype(np.float64)
    b = quantize(csi * 7.5).astype(np.float64)
    np.testing.assert_allclose(a, b, atol=1.0)


# -- the uplink datagram -----------------------------------------------------------------


def test_datagram_parses_with_the_servers_own_parser():
    csi = sample_csi(256)
    datagram = encode_uplink(
        node_id=20,
        seq=7,
        timestamp_us=1234567,
        rssi=-61,
        noise_floor=-95,
        channel=36,
        sec_channel=csi_node.SEC_CHANNEL_ABOVE,
        data=quantize(csi),
        src_mac=AP_MAC,
        link_epoch=3,
    )

    frame = parse_frame(datagram)
    assert frame.node_id == 20
    assert frame.seq == 7
    assert frame.timestamp == 1234567
    assert frame.rssi == -61
    assert frame.noise_floor == -95
    assert frame.channel == 36
    assert frame.n_sub == 256
    assert frame.data.size == 512
    assert frame.src_mac == AP_MAC
    assert frame.link_epoch == 3


def test_the_node_emits_the_current_wire_version():
    """A version the server does not expect is rejected one warning per datagram, so this is
    worth asserting rather than assuming."""
    assert csi_node.WIRE_VERSION == protocol.VERSION
    assert csi_node._WIRE_HEADER.size == protocol.HEADER_SIZE


def test_amplitude_survives_the_round_trip():
    """The server derives amplitude from the bytes we send; it should track the real thing."""
    csi = sample_csi(64)
    frame = parse_frame(
        encode_uplink(
            node_id=20, seq=0, timestamp_us=0, rssi=-50, noise_floor=0,
            channel=6, sec_channel=0, data=quantize(csi),
        )
    )
    got = frame.amplitude()
    # Scaled so the 95th percentile lands on QUANT_PEAK, not the maximum — see quantize().
    want = np.abs(csi) * (csi_node.QUANT_PEAK / np.percentile(np.abs(csi), 95))
    # Bins above that percentile are allowed to clip, so compare where clipping cannot apply.
    unclipped = want < 120.0
    assert unclipped.sum() > 0.8 * csi.size, "the test signal should mostly sit below the clip"
    # int8 quantization on each component, so a couple of counts of slack per bin.
    np.testing.assert_allclose(got[unclipped], want[unclipped], atol=2.0)


def test_one_wild_subcarrier_does_not_crush_the_rest():
    """The regression that made the waterfall look empty.

    This extractor puts a few bins far outside the band's real range — measured, a median of 694
    against 34691 in the guard region. Scaling to the frame maximum spent the whole int8 range on
    those, leaving the actual channel at about two counts, so the server received a signal with
    almost no resolution left in it and the display showed a bright line in the dark.
    """
    csi = np.full(64, 700.0, dtype=np.complex64)
    csi[29] = 34691.0  # the bin this firmware decorates

    data = quantize(csi)
    amplitude = parse_frame(
        encode_uplink(node_id=20, seq=0, timestamp_us=0, rssi=-50, noise_floor=0,
                      channel=6, sec_channel=0, data=data)
    ).amplitude()

    body = np.delete(amplitude, 29)
    assert body.min() > 60, f"the real band was crushed to {body.min():.0f} counts"
    assert amplitude[29] >= body.max(), "the outlier should still read as the largest bin"


def test_rssi_is_clamped_into_the_int8_the_wire_carries():
    frame = parse_frame(
        encode_uplink(
            node_id=1, seq=0, timestamp_us=0, rssi=-4000, noise_floor=9000,
            channel=1, sec_channel=0, data=quantize(sample_csi(64)),
        )
    )
    assert frame.rssi == -128
    assert frame.noise_floor == 127


# -- the node --------------------------------------------------------------------------


def test_sequence_numbers_are_minted_not_copied():
    """nexmon reports the 802.11 sequence number: 12 bits, wrapping, and the transmitter's.

    Passed through it would wrap every 4096 frames — about 41 s at 100 Hz — and each wrap
    would read as a node reboot.
    """
    node = locked_node(node_id=20)
    seqs = []
    for i in range(10):
        # A hostile 802.11 counter: constant, then wrapping backwards.
        wire_seq = 4095 if i % 2 else 0
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), seq=wire_seq)),
            capture_us=i * 10_000,
            now=i * 0.01,
        )
        seqs.append(parse_frame(datagram).seq)

    assert seqs == list(range(10))


def test_timestamps_are_relative_to_the_first_frame():
    node = locked_node(node_id=20)
    base = 1_700_000_000_000_000
    first = node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), capture_us=base, now=0.0)
    later = node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi())), capture_us=base + 12_500, now=0.0125
    )

    assert parse_frame(first).timestamp == 0
    assert parse_frame(later).timestamp == 12_500


def test_only_the_configured_core_and_stream_are_forwarded():
    """One UDP packet arrives per core and spatial stream; taking several would emit several
    frames sharing a moment."""
    node = locked_node(node_id=20, core=0, spatial=0)
    assert node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), core=1)), 0, now=0.0) is None
    assert (
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), spatial=1)), 0, now=0.0) is None
    )
    assert node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 0, now=0.0) is not None
    assert node.skipped_stream == 2


def test_a_clean_run_shows_no_gaps_and_no_reboots():
    """The Phase 1 exit criterion, measured the way the server measures it."""
    node = locked_node(node_id=20)
    health = NodeHealth(node_id=20)

    for i in range(500):
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=i * 10_000, now=i * 0.01
        )
        health.observe(parse_frame(datagram), now=1000.0 + i * 0.01)

    assert health.frames == 500
    assert health.gaps == 0
    assert health.missing == 0
    assert health.reboots == 0
    assert health.loss_rate == 0.0


def test_a_roam_bumps_the_epoch_so_the_server_drops_its_baseline():
    """A roam moves the far end of the measured link into a different room. link_epoch is the
    only thing in the frame that says so, and nodes.py treats it exactly as it treats a
    reboot."""
    node = locked_node(node_id=20)
    health = NodeHealth(node_id=20)
    other_ap = bytes.fromhex("aabbccddeeff")

    for i in range(50):
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=i * 10_000, now=i * 0.01
        )
        assert health.observe(parse_frame(datagram), now=1000.0 + i * 0.01) is False

    moved = node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(), src_mac=other_ap)),
        capture_us=50 * 10_000,
        now=0.5,
    )
    frame = parse_frame(moved)
    assert frame.link_epoch == 1
    assert frame.src_mac == other_ap
    assert health.observe(frame, now=1000.5) is True
    assert node.link_changes == 1
    # A roam, not a reboot: reporting both for one event would double-count it, and the clock
    # and counter are what distinguish them.
    assert health.reboots == 0
    assert frame.seq == 50
    assert frame.timestamp == 500_000


def test_the_epoch_only_moves_when_the_link_does():
    node = locked_node(node_id=20)
    epochs = set()
    for i in range(30):
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), seq=i)), capture_us=i * 10_000, now=i * 0.01
        )
        epochs.add(parse_frame(datagram).link_epoch)
    assert epochs == {0}


def test_the_transmitter_reaches_the_frame():
    """src_mac is the one v2 field a nexmon packet answers directly."""
    node = locked_node(node_id=20)
    frame = parse_frame(node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 0, now=0.0))
    assert frame.src_mac == AP_MAC


def test_a_channel_switch_also_counts_as_a_new_link():
    node = locked_node(node_id=20)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), chanspec=0x1006)), 0, now=0.0)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), chanspec=0x100B)), 10_000, now=0.01)
    assert node.link_changes == 1


def test_link_stays_the_same_when_nothing_changes():
    node = locked_node(node_id=20)
    for i in range(20):
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), seq=i)), capture_us=i * 10_000, now=i * 0.01
        )
    assert node.link_changes == 0


# -- which transmitter, and which kind of frame ------------------------------------------


def test_only_the_configured_transmitter_is_kept():
    """The firmware has a -m filter, but it is not enforced in every build — on bcm43455c0
    7.45.189 in monitor mode everything on the channel arrives. Without a filter somewhere,
    src_mac changes with nearly every frame, link_epoch climbs forever, and the server throws
    the node's history away as fast as it accumulates."""
    node = locked_node(node_id=20, ap_mac=AP_MAC)
    other = bytes.fromhex("aabbccddeeff")

    assert node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(), src_mac=other)), 0, now=0.0
    ) is None
    assert node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 10_000, now=0.01) is not None
    assert node.skipped_transmitter == 1
    assert node.link_changes == 0


def test_frames_of_another_class_are_dropped_once_a_class_is_locked():
    """Block acks and QoS data measure visibly different channels: their medians correlate only
    0.918 with each other while each self-correlates above 0.99. Interleaving them does not
    average out, it puts one stray row into the waterfall every time the rare kind appears."""
    node = locked_node(node_id=20)

    assert node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(), fctl=BLOCK_ACK)), 0, now=0.0
    ) is not None
    assert node.fctl == BLOCK_ACK
    assert node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(), fctl=QOS_DATA)), 10_000, now=0.01
    ) is None
    assert node.skipped_class == 1
    assert node.skipped_width == 0


def test_a_frame_of_the_locked_class_at_another_width_is_counted_separately():
    """A different width is a different subcarrier layout, so it is a different measurement even
    when the frame kind matches — and the counters separate the two so a status line says which
    is happening."""
    node = locked_node(node_id=20)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(64))), 0, now=0.0)

    assert node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(128))), 10_000, now=0.01
    ) is None
    assert node.skipped_width == 1
    assert node.skipped_class == 0


def test_nothing_is_emitted_while_the_class_is_still_being_learned():
    """The server calibrates its baseline on the first frames it sees, and a baseline built from
    a mixture of frame kinds describes no channel. So the window emits nothing at all."""
    node = Node(node_id=20)  # the real window: 400 frames or 2 seconds
    for i in range(20):
        assert node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=i * 10_000, now=i * 0.01
        ) is None
    assert node.fctl is None and node.n_sub is None


def test_the_window_locks_onto_whichever_class_dominates():
    """Which kind to keep is not knowable in advance — it depends on what the traffic provokes —
    so the node counts for a couple of seconds and keeps the majority."""
    node = Node(node_id=20)
    # The measured mixture: ~98% block acks, ~2% QoS data.
    for i in range(99):
        fctl = QOS_DATA if i % 50 == 0 else BLOCK_ACK
        assert node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), fctl=fctl)),
            capture_us=i * 10_000,
            now=i * 0.01,
        ) is None

    # Past learn_seconds, so the next frame closes the window and is judged against the lock.
    emitted = node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(), fctl=BLOCK_ACK)),
        capture_us=99 * 10_000,
        now=LEARN_SECONDS + 0.01,
    )
    assert (node.fctl, node.n_sub) == (BLOCK_ACK, 64)
    assert emitted is not None


def test_the_window_closes_on_frame_count_without_waiting_out_the_clock():
    """Either bound closes it: a busy link should not spend two seconds silent."""
    node = Node(node_id=20, learn_frames=10, learn_seconds=LEARN_SECONDS)
    for i in range(9):
        assert node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=i * 1_000, now=i * 0.001
        ) is None
    assert node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi())), capture_us=9_000, now=0.009
    ) is not None
    assert node.n_sub == 64


def test_a_thin_sample_does_not_close_the_window_when_the_clock_runs_out():
    """The elapsed test alone once decided the class on a sample of three, when a hand-picked
    radio turned out to carry almost no traffic. A majority of three is not a majority, and a
    sparse link is exactly where a wrong lock is most expensive: there is nothing arriving
    afterwards to reveal the mistake."""
    node = Node(node_id=20)
    for t in (0.0, 2.5, 5.0, 10.0, 20.0):
        assert node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=int(t * 1e6), now=t
        ) is None
    assert t > LEARN_SECONDS  # well past the time bound, and still learning
    assert node.n_sub is None


def test_a_sparse_link_locks_eventually_rather_than_never():
    """Not forever, though: a link that never reaches the minimum still has to be measured, so
    past learn_max_seconds it locks on what it has and says the sample was thin."""
    node = Node(node_id=20)
    for t in (0.0, 2.5, 5.0, 10.0, 20.0):
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), capture_us=int(t * 1e6), now=t)

    emitted = node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi())),
        capture_us=int((LEARN_MAX_SECONDS + 1.0) * 1e6),
        now=LEARN_MAX_SECONDS + 1.0,
    )
    assert 6 < LEARN_MIN  # it locked on far fewer frames than the minimum
    assert (node.fctl, node.n_sub) == (BLOCK_ACK, 64)
    assert emitted is not None


def test_a_rare_width_does_not_capture_the_lock():
    """Why the window runs even when the class is configured.

    The width is never configured, so with the window closed the very first frame would decide
    it — and about 1% of frames arrive 65 subcarriers wide. One of those would lock the node to
    a width that then almost never recurs, and nothing would relearn, so it would reject every
    correct frame for the life of the process while logging a confident "0 frames".
    """
    node = Node(node_id=20, fctl=BLOCK_ACK)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(65))), capture_us=0, now=0.0)
    for i in range(1, 60):
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(64))), capture_us=i * 10_000, now=i * 0.01
        )
    node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(64))), capture_us=600_000, now=LEARN_SECONDS + 0.01
    )
    assert (node.fctl, node.n_sub) == (BLOCK_ACK, 64)


def test_the_width_is_learned_within_the_configured_class_not_across_the_air():
    """With --fctl set, taking the overall majority width would read it off whatever else is on
    the channel rather than off the frames being kept."""
    node = Node(node_id=20, fctl=QOS_DATA)
    for i in range(60):
        # A flood of 256-wide block acks around a trickle of 64-wide QoS data.
        pkt = (nexmon_packet(sample_csi(64), fctl=QOS_DATA) if i % 10 == 0
               else nexmon_packet(sample_csi(256), fctl=BLOCK_ACK))
        node.on_packet(parse_nexmon(pkt), capture_us=i * 10_000, now=i * 0.01)
    node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(64), fctl=QOS_DATA)),
        capture_us=600_000,
        now=LEARN_SECONDS + 0.01,
    )
    assert (node.fctl, node.n_sub) == (QOS_DATA, 64)


def test_a_lock_that_outlives_its_class_is_given_up():
    """A lock nothing matches any more is indistinguishable from a dead radio: the node reports
    zero frames while frames keep arriving. It happens for ordinary reasons — the AP changes
    bandwidth, or whatever provoked that kind of frame stops — so the lock is given up."""
    node = locked_node(node_id=20, relearn_seconds=RELEARN_SECONDS)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), fctl=BLOCK_ACK)), 0, now=0.0)
    assert node.fctl == BLOCK_ACK

    # Only the other class from here on. The clock has to run past relearn_seconds.
    for i in range(1, 5):
        assert node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), fctl=QOS_DATA)),
            capture_us=i * 10_000,
            now=i * (RELEARN_SECONDS / 4),
        ) is None

    # The lock was dropped, and with a one-frame window the next frame re-locks onto what is
    # actually arriving.
    emitted = node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(), fctl=QOS_DATA)),
        capture_us=60_000,
        now=RELEARN_SECONDS + 1.0,
    )
    assert node.fctl == QOS_DATA
    assert emitted is not None


def test_a_configured_class_survives_relearning():
    """Relearning must not throw away what was asked for — otherwise a link that goes quiet for
    twenty seconds silently switches the node onto a different measurement."""
    node = locked_node(node_id=20, fctl=BLOCK_ACK, relearn_seconds=RELEARN_SECONDS)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), fctl=BLOCK_ACK)), 0, now=0.0)

    for i in range(1, 6):
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), fctl=QOS_DATA)),
            capture_us=i * 10_000,
            now=i * (RELEARN_SECONDS / 4),
        )
    assert node.fctl == BLOCK_ACK
    assert node.n_sub is None  # the width is up for grabs again, the class is not


# -- the rate limit ----------------------------------------------------------------------


def test_the_rate_limit_holds_the_average_at_the_setting():
    """A deadline, not a gap since the last frame kept.

    Requiring a clear gap sounds equivalent and systematically undershoots: arrivals are random,
    so the first frame past the gap is on average half an inter-arrival late, and every one of
    those delays is inherited by the next. Measured on the Pi, a 120 Hz limit settled at 85.
    """
    rng = np.random.default_rng(1234)
    node = locked_node(node_id=20, max_hz=100.0)

    now_us = 0
    kept = 0
    # Thirty seconds of Poisson arrivals at 300 Hz, which is what a busy access point looks
    # like: bursty, and well above the limit.
    while now_us < 30_000_000:
        now_us += int(rng.exponential(1e6 / 300.0))
        if node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=now_us, now=now_us / 1e6
        ) is not None:
            kept += 1

    rate = kept / (now_us / 1e6)
    assert 97.0 < rate < 101.0, f"{rate:.1f} Hz"


def test_the_rate_limit_does_not_advance_the_sequence_counter():
    """A dropped frame must cost nothing but itself. If seq or the epoch moved for a frame the
    server never sees, every drop would read as loss."""
    node = locked_node(node_id=20, max_hz=100.0)
    kept = [
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=i * 1_000, now=i * 0.001
        )
        for i in range(50)
    ]
    seqs = [parse_frame(d).seq for d in kept if d is not None]
    assert seqs == list(range(len(seqs)))
    assert node.skipped_rate == 50 - len(seqs)


def test_the_deadline_gives_up_after_a_real_outage():
    """Running behind is how the average is held; running minutes behind is not. After a gap
    the credit is forfeited, or everything arriving would be emitted at full speed."""
    node = locked_node(node_id=20, max_hz=100.0)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), capture_us=0, now=0.0)

    # Ten minutes of nothing, then a burst arriving far faster than the limit.
    base = 600_000_000
    burst = [
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())),
            capture_us=base + i * 1_000,
            now=(base + i * 1_000) / 1e6,
        )
        for i in range(30)
    ]
    assert sum(d is not None for d in burst) <= 4


# -- what the probes are actually buying ---------------------------------------------------

AUDIT_WINDOW = 5.0
# What the audit waits before the first one, however long the interval is.
FIRST_WAIT = 60.0


def drive_audit(audit: ProbeAudit, *, total_hz: float, background_hz: float, until: float,
                sent_hz: float = 100.0, step: float = 0.5) -> list[tuple[float, bool]]:
    """Run `audit` over a synthetic timeline; returns [(t, probing), ...].

    Frames arrive at `total_hz` while the audit says to probe and at `background_hz` while it
    says not to, which is the whole thing being modelled: the difference between the two is what
    the probes are buying, and the audit's job is to recover it without being told.
    """
    frames = 0
    sent = 0
    t = 0.0
    probing = audit.poll(t, frames, sent)
    timeline = [(t, probing)]
    while t < until:
        frames += round((total_hz if probing else background_hz) * step)
        if probing:
            sent += round(sent_hz * step)
        t += step
        probing = audit.poll(t, frames, sent)
        timeline.append((t, probing))
    return timeline


def test_the_audit_recovers_the_induced_rate_from_the_difference():
    """The measured numbers from this hardware: 104 Hz arriving with the probes, 26 Hz without.

    Neither is knowable in advance. The 26 Hz is the household's own traffic to that access
    point, which at four in the morning is nothing and at eight in the evening is most of the
    rate; the 78 Hz is what the probes provoke, which depends on the mode and on whether this
    particular access point puts anything on the air in response at all.
    """
    audit = ProbeAudit(300.0, window_s=AUDIT_WINDOW, mode="broadcast")
    drive_audit(audit, total_hz=104.0, background_hz=26.0, until=100.0)

    assert audit.audits == 1
    assert audit.total_hz == pytest.approx(104.0)
    assert audit.background_hz == pytest.approx(26.0)
    assert audit.induced_hz == pytest.approx(78.0)
    assert audit.share == pytest.approx(0.75)
    assert audit.describe() == (
        "probe yield: 78 Hz induced of 104 Hz total (75%), background 26 Hz"
    )


def test_the_probes_stop_for_exactly_one_window():
    """The cost of the answer. Every second of quiet is a second the server is fed at the
    background rate, so the window is short and the interval between them is long."""
    audit = ProbeAudit(300.0, window_s=AUDIT_WINDOW)
    timeline = drive_audit(audit, total_hz=104.0, background_hz=26.0, until=100.0, step=0.5)

    quiet = [t for t, probing in timeline if not probing]
    # The first audit comes at FIRST_WAIT rather than after a whole interval, and the quiet half
    # follows the probing half.
    assert quiet[0] == FIRST_WAIT + AUDIT_WINDOW
    assert quiet[-1] == FIRST_WAIT + 2 * AUDIT_WINDOW - 0.5
    assert len(quiet) == AUDIT_WINDOW / 0.5


def test_the_first_audit_does_not_wait_out_a_whole_interval():
    """Deployment is when the question is live: 'is this probing doing anything on this site?'
    An hourly audit that answers it in an hour answers it too late to act on."""
    audit = ProbeAudit(3600.0, window_s=AUDIT_WINDOW)
    drive_audit(audit, total_hz=104.0, background_hz=26.0, until=120.0)
    assert audit.audits == 1
    assert audit.induced_hz == pytest.approx(78.0)


def test_a_disabled_audit_never_stops_the_probes():
    audit = ProbeAudit(0.0)
    assert not audit.enabled
    timeline = drive_audit(audit, total_hz=104.0, background_hz=26.0, until=1000.0)
    assert all(probing for _, probing in timeline)
    assert audit.audits == 0
    assert audit.describe() == "probe yield: not measured yet"


def test_probing_that_buys_nothing_is_reported_as_buying_nothing(caplog):
    """The answer this feature exists for, and the one a wired uplink is expected to give.

    An access point sends group-addressed frames at a legacy basic rate and this extractor
    produces CSI only for HT/VHT frames, so a broadcast that provokes no acknowledgement to *us*
    is not measurable however many are sent. The warning has to name that, and name the thing to
    try instead, or the operator reads a plausible 26 Hz and believes the node is working.
    """
    audit = ProbeAudit(60.0, window_s=AUDIT_WINDOW, mode="broadcast")
    with caplog.at_level(logging.WARNING, logger="csi.pi"):
        drive_audit(audit, total_hz=26.0, background_hz=26.0, until=100.0)

    assert audit.audits == 1
    assert audit.share == 0.0
    assert "buying nothing measurable" in caplog.text
    assert "legacy basic rate" in caplog.text
    assert "--probe-mode unicast" in caplog.text


@pytest.mark.parametrize(
    "mode,cause",
    [
        # Unicast is only measurable when the access point has to put the packet on the air.
        ("unicast", "reached over the wire"),
        # icmp replies are rate-limited by the remote stack and aggregated on the way back.
        ("icmp", "rate-limits them"),
    ],
)
def test_the_warning_names_the_cause_for_the_mode_in_use(mode, cause, caplog):
    audit = ProbeAudit(60.0, window_s=AUDIT_WINDOW, mode=mode)
    with caplog.at_level(logging.WARNING, logger="csi.pi"):
        drive_audit(audit, total_hz=26.0, background_hz=26.0, until=100.0)
    assert mode in caplog.text.lower()
    assert cause in caplog.text
    assert "--ap" in caplog.text


def test_probes_that_never_left_the_machine_are_named_before_the_mode_is_blamed(caplog):
    """A socket failing every send and an access point ignoring every probe look identical from
    the frame rate, and have nothing in common to fix."""
    audit = ProbeAudit(60.0, window_s=AUDIT_WINDOW, mode="broadcast")
    with caplog.at_level(logging.WARNING, logger="csi.pi"):
        drive_audit(audit, total_hz=26.0, background_hz=26.0, sent_hz=0.0, until=100.0)

    assert audit.sent_hz == 0.0
    assert "No probe actually left this machine" in caplog.text
    assert "legacy basic rate" not in caplog.text


def test_an_audit_that_saw_no_frames_at_all_blames_the_capture_not_the_probes(caplog):
    """Both halves zero says nothing about probing — it says the capture is dead — and reporting
    it as a probe fault would send someone to change --probe-mode over a wrong chanspec."""
    audit = ProbeAudit(60.0, window_s=AUDIT_WINDOW, mode="broadcast")
    with caplog.at_level(logging.WARNING, logger="csi.pi"):
        drive_audit(audit, total_hz=0.0, background_hz=0.0, until=100.0)

    assert "no frames arrived at all" in caplog.text
    assert "buying nothing measurable" not in caplog.text


def test_a_background_above_the_probed_rate_is_reported_honestly_as_no_yield():
    """The two halves are ten seconds of somebody else's traffic, so the difference can come out
    negative. That is noise, not a negative yield: report the numbers as measured and let the
    share bottom out at zero."""
    audit = ProbeAudit(60.0, window_s=AUDIT_WINDOW)
    drive_audit(audit, total_hz=20.0, background_hz=26.0, until=100.0)

    assert audit.induced_hz == pytest.approx(-6.0)
    assert audit.share == 0.0
    assert audit.describe().startswith("probe yield: -6 Hz induced of 20 Hz total (0%)")


def test_the_status_line_carries_the_last_yield_and_says_when_the_probes_are_off():
    """The audit reports every few minutes and the status line every few seconds, so the line
    has to carry the last answer — and has to explain the dip while an audit is in progress,
    because an unexplained halving of the rate reads as the link failing."""
    audit = ProbeAudit(300.0, window_s=AUDIT_WINDOW)
    assert csi_node._audit_suffix(None) == ""
    assert csi_node._audit_suffix(ProbeAudit(0.0)) == ""
    assert csi_node._audit_suffix(audit).endswith("not measured yet")

    drive_audit(audit, total_hz=104.0, background_hz=26.0, until=100.0)
    suffix = csi_node._audit_suffix(audit)
    assert "78 Hz induced of 104 Hz total (75%)" in suffix
    assert "probes stopped" not in suffix

    # And a line printed while the next audit's quiet half is in progress: the rate on it is
    # the background, which has to be said or the dip reads as the link failing.
    mid_audit = ProbeAudit(60.0, window_s=AUDIT_WINDOW)
    drive_audit(mid_audit, total_hz=104.0, background_hz=26.0, until=137.0)
    assert mid_audit.quiet and mid_audit.audits == 1
    suffix = csi_node._audit_suffix(mid_audit)
    assert "78 Hz induced" in suffix
    assert "probes stopped for an audit" in suffix


def test_the_audit_counts_supply_rather_than_what_the_rate_limiter_kept():
    """Why Node.eligible is counted before the limiter and not after.

    The limiter exists to flatten a surplus. Measured downstream of it, a link carrying 300 Hz
    and one carrying 110 Hz both read as the 100 Hz cap — so the audit would subtract one from
    the other, find nothing, and report that probing does nothing at the exact moment it was
    working.
    """
    node = locked_node(node_id=20, max_hz=100.0)
    for i in range(300):  # one second of 300 Hz supply
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=i * 3_333, now=i * 0.003_333
        )

    assert node.eligible == 300
    assert node.frames < 120  # the limiter kept about a hundred of them
    assert node.skipped_rate == 300 - node.frames


def test_frames_that_are_not_the_measurement_are_not_counted_as_supply():
    """Supply means frames of the thing being measured. Counting another transmitter's, or
    another frame class's, would let a busy channel disguise probes that provoke nothing."""
    node = locked_node(node_id=20, ap_mac=AP_MAC)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 0, now=0.0)
    assert node.eligible == 1

    other = bytes.fromhex("aabbccddeeff")
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), src_mac=other)), 10_000, now=0.01)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), fctl=QOS_DATA)), 20_000, now=0.02)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(128))), 30_000, now=0.03)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), core=1)), 40_000, now=0.04)
    assert node.eligible == 1

    node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 50_000, now=0.05)
    assert node.eligible == 2


def test_the_quiet_window_is_a_gap_in_supply_not_a_discontinuity():
    """The constraint the audit is built around, checked the way the server checks it.

    Stopping the probes is allowed to cost frames. It is not allowed to change what a frame
    means, so the sequence counter runs straight through the hole and the link epoch does not
    move — the server has to see a lull it can resample across, not a reboot that throws the
    baseline away.

    The other half of it is the rate limiter's deadline, which runs deliberately behind so that
    the average comes out at the setting. Five seconds behind is not a debt worth repaying: it
    resyncs past three intervals, so the frames that arrive when probing resumes are paced
    rather than dumped at supply rate.
    """
    node = locked_node(node_id=20, max_hz=100.0)
    health = NodeHealth(node_id=20)

    def feed(capture_us: int):
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=capture_us,
            now=capture_us / 1e6,
        )
        if datagram is not None:
            frame = parse_frame(datagram)
            health.observe(frame, now=1000.0 + capture_us / 1e6)
            return frame
        return None

    # Five seconds of 300 Hz supply, then five seconds of the quiet window during which this
    # link produces nothing at all, then supply again.
    for us in range(0, 5_000_000, 3_333):
        feed(us)
    before = node.frames

    after_the_gap = [f for us in range(10_000_000, 10_500_000, 3_333) if (f := feed(us))]

    assert node.epoch == 0
    assert node.link_changes == 0 and node.class_changes == 0
    assert health.roams == 0 and health.reboots == 0
    # Contiguous across the hole: nothing the server can read as loss.
    assert health.gaps == 0 and health.missing == 0
    assert [f.link_epoch for f in after_the_gap] == [0] * len(after_the_gap)
    assert after_the_gap[0].seq == before

    # Half a second of supply at 300 Hz on the far side of the hole. Paced, that is ~50 frames;
    # a deadline catching up on five seconds of credit would have emitted all 150.
    assert 40 <= len(after_the_gap) <= 55


# -- a change of frame class is a change of measurement -------------------------------------


def test_a_frame_class_change_bumps_the_epoch():
    """Re-locking onto a different class is not a gap, it is a different measurement.

    The classes measure visibly different channels — their median profiles correlate 0.918 here
    against better than 0.99 within a class — so every baseline built on the old class describes
    the old measurement. That is what link_epoch is for, and nodes.py answers it exactly as it
    answers a roam.
    """
    node = locked_node(node_id=20, relearn_seconds=RELEARN_SECONDS)
    health = NodeHealth(node_id=20)

    first = parse_frame(
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), fctl=BLOCK_ACK)), 0, now=0.0)
    )
    assert first.link_epoch == 0
    assert health.observe(first, now=1000.0) is False

    # Only the other class from here, until the lock is given up.
    for i in range(1, 5):
        assert node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), fctl=QOS_DATA)),
            capture_us=i * 10_000,
            now=i * (RELEARN_SECONDS / 4),
        ) is None

    relocked = parse_frame(
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), fctl=QOS_DATA)),
            capture_us=60_000,
            now=RELEARN_SECONDS + 1.0,
        )
    )

    assert node.fctl == QOS_DATA
    assert node.class_changes == 1
    # Charged on a frame the server actually receives, so the epoch it carries is the new one.
    assert relocked.link_epoch == 1
    assert health.observe(relocked, now=1000.0 + RELEARN_SECONDS + 1.0) is True
    # A change of measurement, not a change of link: the transmitter never moved.
    assert node.link_changes == 0
    assert health.reboots == 0


def test_the_first_class_lock_is_not_a_class_change():
    """There is nothing behind the first lock to invalidate, and an epoch bump on the very first
    frame would tell the server to throw away a baseline it has not built yet."""
    node = locked_node(node_id=20)
    first = parse_frame(node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 0, now=0.0))
    assert first.link_epoch == 0
    assert node.class_changes == 0


def test_relocking_onto_the_same_class_is_not_a_class_change():
    """A lock given up and then regained on the same (class, width) measured the same channel
    throughout. Bumping the epoch there would cost the server its baseline for what was only a
    quiet spell — which is precisely what an audit's quiet window looks like."""
    node = locked_node(node_id=20, fctl=BLOCK_ACK, relearn_seconds=RELEARN_SECONDS)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(64))), 0, now=0.0)
    assert (node.fctl, node.n_sub) == (BLOCK_ACK, 64)

    # The right class at the wrong width for long enough to lose the lock.
    for i in range(1, 5):
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(128))),
            capture_us=i * 10_000,
            now=i * (RELEARN_SECONDS / 4),
        )
    assert node.n_sub is None

    regained = parse_frame(
        node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(64))),
            capture_us=100_000,
            now=RELEARN_SECONDS + 1.0,
        )
    )
    assert (node.fctl, node.n_sub) == (BLOCK_ACK, 64)
    assert node.class_changes == 0
    assert regained.link_epoch == 0


# -- pausing the prober --------------------------------------------------------------------


def test_the_prober_can_be_paused_without_being_stopped():
    """A thread cannot be restarted, and reopening the socket every audit would make the audit
    itself a variable in what it measures."""
    prober = csi_node.Prober("192.0.2.1", 100.0)
    assert prober.probing

    prober.pause()
    assert not prober.probing
    prober.pause()  # idempotent, so the caller never has to track which state it asked for
    assert not prober.probing

    prober.resume()
    assert prober.probing


def test_stopping_wakes_a_paused_prober():
    """Otherwise shutdown waits out the pause poll for no reason."""
    prober = csi_node.Prober("192.0.2.1", 100.0)
    prober.pause()
    prober.stop()
    assert prober.probing  # released, so the send loop reaches its stop check


# -- signal strength -----------------------------------------------------------------------


def test_the_frames_own_rssi_is_preferred():
    """This build carries a per-frame RSSI, and in monitor mode it is the only one there is:
    the interface is not associated, so /proc/net/wireless does not list it and LinkStats
    reports a confident zero forever."""
    node = locked_node(node_id=20)
    node.rssi, node.noise = 0, 0
    frame = parse_frame(
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), rssi=-63)), 0, now=0.0)
    )
    assert frame.rssi == -63


def test_rssi_from_the_driver_is_the_fallback_when_the_header_byte_is_empty():
    """A build that leaves the byte at zero should fall back rather than report a dBm of 0."""
    node = locked_node(node_id=20)
    node.rssi, node.noise = -58, -92
    frame = parse_frame(
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), rssi=0)), 0, now=0.0)
    )
    assert frame.rssi == -58
    assert frame.noise_floor == -92


@pytest.mark.parametrize("implausible", [0, 5, -120])
def test_an_implausible_header_rssi_falls_back(implausible):
    node = locked_node(node_id=20)
    node.rssi = -58
    frame = parse_frame(
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), rssi=implausible)), 0, now=0.0)
    )
    assert frame.rssi == -58


# -- reading the driver's link stats -----------------------------------------------------

PROC_WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   70.  -40.  -92.       0      0      0      0      0        0
"""


def test_reads_level_and_noise(tmp_path):
    path = tmp_path / "wireless"
    path.write_text(PROC_WIRELESS)
    assert read_wireless(str(path), "wlan0") == (-40, -92)


def test_absent_noise_estimate_is_reported_as_zero(tmp_path):
    """Drivers with no noise figure report -256, which is not a noise floor."""
    path = tmp_path / "wireless"
    path.write_text(PROC_WIRELESS.replace("-92.", "-256"))
    assert read_wireless(str(path), "wlan0") == (-40, 0)


def test_unknown_interface_reads_as_none(tmp_path):
    path = tmp_path / "wireless"
    path.write_text(PROC_WIRELESS)
    assert read_wireless(str(path), "wlan1") is None


def test_missing_proc_file_reads_as_none(tmp_path):
    assert read_wireless(str(tmp_path / "nope"), "wlan0") is None


# -- packet plumbing ---------------------------------------------------------------------


def ipv4_udp(payload: bytes, *, dport: int = 5500, proto: int = 17, ihl: int = 5) -> bytes:
    header = bytes([0x40 | ihl, 0, 0, 0, 0, 0, 0, 0, 64, proto, 0, 0]) + bytes(8)
    header = header[: ihl * 4].ljust(ihl * 4, b"\x00")
    udp = struct.pack("!HHHH", 1234, dport, 8 + len(payload), 0)
    return header + udp + payload


def test_udp_payload_extracts_the_right_port():
    assert udp_payload(ipv4_udp(b"hello"), 5500) == b"hello"


@pytest.mark.parametrize(
    "packet",
    [
        b"",
        b"\x45" + bytes(10),  # truncated
        ipv4_udp(b"hello", dport=5501),  # wrong port
        ipv4_udp(b"hello", proto=6),  # TCP
    ],
)
def test_udp_payload_rejects_everything_else(packet):
    assert udp_payload(packet, 5500) is None


def test_udp_payload_honours_a_long_ip_header():
    """IHL is in 32-bit words and options are legal; a fixed 20 would read into the payload."""
    assert udp_payload(ipv4_udp(b"hello", ihl=7), 5500) == b"hello"


def scm_timestamp(data: bytes):
    import socket as s

    return [(s.SOL_SOCKET, csi_node.SCM_TIMESTAMPNS, data)]


def test_timestamp_us_reads_the_kernel_control_message():
    assert timestamp_us(scm_timestamp(struct.pack("=qq", 1700, 250_000_000))) == (
        1700 * 1_000_000 + 250_000
    )


def test_timestamp_us_reads_a_32_bit_timespec():
    """The width follows userspace, not the kernel. SO_TIMESTAMPNS is the legacy option, so on
    32-bit ARM the kernel hands back the 8-byte `timespec32` — and 32-bit Raspberry Pi OS is
    the default image for every Pi that is not a 4 or a 5.

    Demanding 16 bytes there breaks nothing visibly: every frame falls through to the caller's
    wall-clock fallback, so timestamps stay monotonic and roughly right while quietly picking
    up this process's scheduling jitter. The breathing estimator resamples on exactly these
    timestamps and cannot repair one that is wrong.
    """
    assert timestamp_us(scm_timestamp(struct.pack("=ii", 1700, 250_000_000))) == (
        1700 * 1_000_000 + 250_000
    )


def test_timestamp_us_is_none_for_a_width_it_does_not_recognise():
    """Better the caller's loud fallback than a number decoded out of the wrong bytes."""
    assert timestamp_us(scm_timestamp(bytes(12))) is None


def test_timestamp_us_is_none_when_the_kernel_did_not_stamp():
    assert timestamp_us([]) is None
    assert timestamp_us([(0, 0, b"junk")]) is None


# -- argument handling -------------------------------------------------------------------


def test_reserved_node_ids_are_rejected():
    for bad in ("0", "255"):
        with pytest.raises(SystemExit):
            csi_node.parse_args(["--node-id", bad])


def test_node_id_and_server_are_taken_from_the_command_line():
    args = csi_node.parse_args(["--node-id", "42", "--server", "10.0.0.5", "--port", "9999"])
    assert (args.node_id, args.server, args.port) == (42, "10.0.0.5", 9999)


def test_the_audit_can_be_switched_off():
    args = csi_node.parse_args(["--probe-audit-interval", "0"])
    assert args.probe_audit_interval == 0


def test_the_audit_defaults_leave_room_for_both_halves():
    args = csi_node.parse_args([])
    assert args.probe_audit_interval == 300.0
    assert args.probe_audit_window == 5.0


def test_an_audit_interval_too_short_for_its_own_windows_is_rejected():
    with pytest.raises(SystemExit):
        csi_node.parse_args(["--probe-audit-interval", "8", "--probe-audit-window", "5"])


def test_a_quiet_window_longer_than_the_class_lock_waits_is_rejected():
    """Past relearn_seconds the quiet window stops being a gap in supply: whatever class the
    probes were provoking has been absent long enough that the lock is given up and learned
    again, which bumps the link epoch. An audit is not allowed to do that."""
    with pytest.raises(SystemExit):
        csi_node.parse_args(["--probe-audit-window", str(RELEARN_SECONDS)])


def test_the_capture_and_probe_interfaces_are_separate_settings():
    """They have to be. The capture radio is in monitor mode and cannot transmit, so the probes
    leave by the interface holding the association — the USB dongle."""
    args = csi_node.parse_args(["--iface", "wlan0", "--probe-iface", "wlan1"])
    assert (args.iface, args.probe_iface) == ("wlan0", "wlan1")


@pytest.mark.parametrize(
    "text,want",
    [
        ("00:11:22:33:44:55", AP_MAC),
        ("00-11-22-33-44-55", AP_MAC),
        ("001122334455", AP_MAC),
        (None, None),
        ("", None),
    ],
)
def test_parse_mac_accepts_the_forms_a_config_file_holds(text, want):
    assert parse_mac(text) == want


def test_parse_mac_rejects_something_that_is_not_a_mac():
    """A half-read BSSID written into /etc/default/csi-node has to fail loudly: silently
    filtering on the wrong transmitter looks exactly like a quiet room."""
    with pytest.raises(ValueError):
        parse_mac("00:11:22:33:44")


def test_icmp_echo_checksums_to_zero():
    """A correct checksum is the difference between the AP replying and ignoring us."""
    packet = csi_node.icmp_echo(0x1234, 7)
    assert csi_node.checksum(packet) == 0
