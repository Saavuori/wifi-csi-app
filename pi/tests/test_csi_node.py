"""Tests for the Raspberry Pi node.

The load-bearing ones feed synthetic nexmon packets through the node and parse the result with
the *server's* own protocol module. That is what stops the fourth copy of the wire header from
drifting away from the other three, and it means the translation can be exercised without a Pi.
"""

from __future__ import annotations

import struct

import csi_node
import numpy as np
import pytest

# The server is a dependency of the test run, not of the node itself.
from csi import protocol
from csi.nodes import NodeHealth
from csi.protocol import parse_frame
from csi_node import (
    NARROWBAND_BLOCK,
    NEXMON_HEADER_SIZE,
    Node,
    RateGate,
    chanspec_channel,
    chanspec_sec_channel,
    encode_uplink,
    occupied_span,
    parse_nexmon,
    quantize,
    read_wireless,
    timestamp_us,
    udp_payload,
)

AP_MAC = bytes.fromhex("001122334455")
# 2.4 GHz channel 6, 20 MHz.
CHANSPEC_CH6 = 0x1006


def nexmon_packet(
    csi: np.ndarray,
    *,
    src_mac: bytes = AP_MAC,
    seq: int = 0,
    core: int = 0,
    spatial: int = 0,
    chanspec: int = CHANSPEC_CH6,
    chanspec_big_endian: bool = False,
    rssi: int = -40,
) -> bytes:
    """Build a nexmon CSI payload. `csi` is complex; real and imag are stored int16."""
    chan = struct.pack(">H" if chanspec_big_endian else "<H", chanspec)
    # magic u16 | rssi i8 | fctl u8 | src_mac 6 | seq u16 | core/spatial u16 | chanspec u16 |
    # chip u16. The rssi/fctl pair used to be missing here *and* in csi_node, so these tests
    # agreed with the parser and both were wrong: a real 80 MHz frame is 1042 bytes, which a
    # 16-byte header turns into 1026 -- not a multiple of 4, so every live frame was rejected
    # while the suite stayed green. Byte counts here now come from a real capture.
    header = (
        struct.pack("<H", csi_node.NEXMON_MAGIC)
        + struct.pack("<b", rssi)
        + struct.pack("<B", 0x80)
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


# -- nexmon decoding ---------------------------------------------------------------------


def test_parses_a_well_formed_packet():
    csi = sample_csi()
    pkt = parse_nexmon(nexmon_packet(csi, core=1, spatial=2))

    assert pkt is not None
    assert pkt.n_sub == 64
    assert pkt.src_mac == AP_MAC
    assert (pkt.core, pkt.spatial) == (1, 2)
    assert chanspec_channel(pkt.chanspec) == 6
    np.testing.assert_allclose(pkt.csi, np.rint(csi.real) + 1j * np.rint(csi.imag))


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


# -- narrowband frames -------------------------------------------------------------------

# 5 GHz channel 36, 80 MHz: the chanspec the Ethernet stimulus is a fallback for.
CHANSPEC_CH36_80 = 0xE12A


def narrowband_csi(block: int = 1, n_sub: int = 256, noise: float = 10.0) -> np.ndarray:
    """A 20 MHz frame as an 80 MHz capture sees it: one block of signal, the rest noise.

    This is what every beacon looks like on a wide chanspec, and what the access point's
    transmission of the Ethernet stimulus looks like — multicast goes out at the basic rate.
    """
    rng = np.random.default_rng(seed=block)
    csi = noise * (rng.normal(size=n_sub) + 1j * rng.normal(size=n_sub))
    lo = block * NARROWBAND_BLOCK
    csi[lo : lo + NARROWBAND_BLOCK] = sample_csi(NARROWBAND_BLOCK)
    return csi


@pytest.mark.parametrize("block", [0, 1, 2, 3])
def test_a_narrowband_frame_is_located(block):
    span = occupied_span(narrowband_csi(block))
    assert span == slice(block * NARROWBAND_BLOCK, (block + 1) * NARROWBAND_BLOCK)


@pytest.mark.parametrize("n_sub", [128, 256])
def test_a_full_width_frame_is_left_whole(n_sub):
    """The damaging false positive: truncating a real measurement to a quarter of itself."""
    assert occupied_span(sample_csi(n_sub)) is None


def test_a_20mhz_capture_has_nothing_to_compare_against():
    assert occupied_span(sample_csi(64)) is None


def test_a_frame_just_inside_the_margin_is_still_full_width():
    """A deeply frequency-selective channel is not a narrowband frame."""
    csi = sample_csi(256)
    csi[:192] *= 10.0 ** (-11.0 / 20.0)  # 11 dB down, one dB inside the 12 dB threshold
    assert occupied_span(csi) is None


def test_the_node_forwards_one_class_at_a_time():
    """Both classes reach the radio at once — beacons are narrow, real traffic is wide. Emitting
    both would flip n_sub frame by frame, and every flip costs the server its history."""
    node = Node(node_id=20)
    narrow = nexmon_packet(narrowband_csi(), chanspec=CHANSPEC_CH36_80)
    wide = nexmon_packet(sample_csi(256), chanspec=CHANSPEC_CH36_80)

    assert node.on_packet(parse_nexmon(narrow), 0) is None
    assert node.on_packet(parse_nexmon(wide), 10_000) is not None
    assert node.dropped_class == 1

    node.narrowband = True
    assert node.on_packet(parse_nexmon(wide), 20_000) is None
    assert node.on_packet(parse_nexmon(narrow), 30_000) is not None
    assert node.dropped_class == 2


def test_a_narrowband_frame_is_trimmed_to_the_block_it_occupies():
    node = Node(node_id=20)
    node.narrowband = True
    frame = parse_frame(
        node.on_packet(parse_nexmon(nexmon_packet(narrowband_csi(), chanspec=CHANSPEC_CH36_80)), 0)
    )
    assert frame.n_sub == NARROWBAND_BLOCK
    # A 20 MHz measurement, whatever the chip is tuned to.
    assert frame.sec_channel == csi_node.SEC_CHANNEL_NONE


def test_the_trimmed_frame_carries_the_signal_and_not_the_noise():
    """The point of trimming: 192 of the 256 bins were noise, and normalizing across them would
    have quietly ruined every frame the stimulus produced."""
    node = Node(node_id=20)
    node.narrowband = True
    csi = narrowband_csi(block=2)
    frame = parse_frame(
        node.on_packet(parse_nexmon(nexmon_packet(csi, chanspec=CHANSPEC_CH36_80)), 0)
    )

    kept = frame.amplitude()
    expected = np.abs(csi[2 * NARROWBAND_BLOCK : 3 * NARROWBAND_BLOCK])
    # Quantization discards absolute gain, so compare the shape rather than the values.
    np.testing.assert_allclose(
        kept / kept.max(), expected / expected.max(), atol=0.02
    )


def test_changing_class_bumps_the_epoch():
    """A width change is a change of instrument, in the same way a roam is a change of room."""
    node = Node(node_id=20)
    wide = parse_frame(
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi(256), chanspec=CHANSPEC_CH36_80)), 0)
    )
    node.narrowband = True
    narrow = parse_frame(
        node.on_packet(
            parse_nexmon(nexmon_packet(narrowband_csi(), chanspec=CHANSPEC_CH36_80)), 10_000
        )
    )

    assert (wide.n_sub, wide.link_epoch) == (256, 0)
    assert (narrow.n_sub, narrow.link_epoch) == (64, 1)
    assert node.link_changes == 1


def test_a_20mhz_link_has_no_classes_to_lock():
    """Nothing at 20 MHz can be told apart from anything else, so neither mode may drop a frame
    and the switch must not throw the baseline away. Locking here would leave `--stimulus
    always` on a 20 MHz link capturing perfectly and forwarding nothing."""
    node = Node(node_id=20)
    packet = nexmon_packet(sample_csi(64))

    assert node.on_packet(parse_nexmon(packet), 0) is not None
    node.narrowband = True
    assert node.on_packet(parse_nexmon(packet), 10_000) is not None
    assert node.dropped_class == 0
    assert node.link_changes == 0


def test_a_20mhz_capture_is_never_evidence_of_other_traffic():
    """So the gate sees silence and leaves the stimulus running — right at this width, where it
    costs no subcarriers, and the alternative is toggling on its own echo."""
    node = Node(node_id=20)
    for i in range(10):
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi(64))), i * 10_000)
    assert node.wideband_seen == 0


def test_only_full_width_frames_count_as_other_peoples_traffic():
    """What the gate reads. Counting our own stimulus here would make it disarm itself."""
    node = Node(node_id=20)
    node.narrowband = True
    for i in range(5):
        node.on_packet(
            parse_nexmon(nexmon_packet(narrowband_csi(), chanspec=CHANSPEC_CH36_80)), i * 10_000
        )
    assert node.wideband_seen == 0

    node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(256), chanspec=CHANSPEC_CH36_80)), 60_000
    )
    # Seen, and counted, even though the mode meant it was not forwarded.
    assert node.wideband_seen == 1
    assert node.dropped_class == 1


# -- the stimulus gate -------------------------------------------------------------------


def gate(**kw) -> RateGate:
    opts = dict(floor_hz=10.0, ceiling_hz=25.0, window_s=5.0, dwell_s=30.0)
    opts.update(kw)
    return RateGate(**opts)


def feed(g: RateGate, *, seconds: float, rate_hz: float, start: float = 0.0) -> tuple[float, int]:
    """Run `seconds` of traffic at `rate_hz` past the gate, one wakeup every 0.5 s."""
    now, seen = start, 0
    steps = int(seconds / 0.5)
    for _ in range(steps):
        now += 0.5
        seen += int(rate_hz * 0.5)
        g.update(now, seen)
    return now, seen


def test_the_gate_arms_when_the_channel_goes_quiet():
    g = gate()
    feed(g, seconds=20, rate_hz=0.0)
    assert g.armed is True
    assert g.changes == 1


def test_the_gate_stays_off_while_there_is_real_traffic():
    g = gate()
    feed(g, seconds=60, rate_hz=100.0)
    assert g.armed is False
    assert g.changes == 0


def test_the_gate_disarms_once_the_household_comes_back():
    g = gate()
    now, seen = feed(g, seconds=20, rate_hz=0.0)
    assert g.armed is True
    # Past the dwell, then real traffic well above the ceiling.
    for _ in range(120):
        now += 0.5
        seen += 25
        g.update(now, seen)
    assert g.armed is False
    assert g.changes == 2


def test_the_hysteresis_band_holds_the_decision():
    """Between the floor and the ceiling nothing changes, in either direction. Without the gap a
    rate hovering at the threshold would toggle the subcarrier count every window."""
    quiet = gate()
    feed(quiet, seconds=20, rate_hz=0.0)
    now, seen = feed(quiet, seconds=200, rate_hz=18.0, start=20.0)
    assert quiet.armed is True  # above the floor, below the ceiling: stays armed

    busy = gate()
    feed(busy, seconds=200, rate_hz=18.0)
    assert busy.armed is False  # same rate, opposite starting point, opposite answer


def test_the_dwell_stops_the_gate_thrashing():
    """Each decision costs the server the node's history, so they have to be rare."""
    g = gate(dwell_s=30.0)
    now, seen = 0.0, 0
    # Alternate silence and a flood every window for five minutes.
    for i in range(600):
        now += 0.5
        seen += 0 if (i // 10) % 2 == 0 else 25
        g.update(now, seen)
    assert g.changes <= 300 / 30 + 1


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
    # Scaled from the data subcarriers, not the whole array — DC and the guards are not
    # transmitted and must not set the scale. See quantize().
    peak = np.max(np.abs(csi)[csi_node.data_bins(csi.size)])
    want = np.abs(csi) * (csi_node.QUANT_PEAK / peak)
    # int8 quantization on each component, so a couple of counts of slack per bin. Bins that
    # clip are excluded: the guards are allowed to saturate now.
    unclipped = want <= 127.0
    np.testing.assert_allclose(got[unclipped], want[unclipped], atol=2.0)


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
    node = Node(node_id=20)
    seqs = []
    for i in range(10):
        # A hostile 802.11 counter: constant, then wrapping backwards.
        wire_seq = 4095 if i % 2 else 0
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), seq=wire_seq)), capture_us=i * 10_000
        )
        seqs.append(parse_frame(datagram).seq)

    assert seqs == list(range(10))


def test_timestamps_are_relative_to_the_first_frame():
    node = Node(node_id=20)
    base = 1_700_000_000_000_000
    first = node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), capture_us=base)
    later = node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), capture_us=base + 12_500)

    assert parse_frame(first).timestamp == 0
    assert parse_frame(later).timestamp == 12_500


def test_only_the_configured_core_and_stream_are_forwarded():
    """One UDP packet arrives per core and spatial stream; taking several would emit several
    frames sharing a moment."""
    node = Node(node_id=20, core=0, spatial=0)
    assert node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), core=1)), 0) is None
    assert node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), spatial=1)), 0) is None
    assert node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 0) is not None
    assert node.skipped_stream == 2


def test_a_clean_run_shows_no_gaps_and_no_reboots():
    """The Phase 1 exit criterion, measured the way the server measures it."""
    node = Node(node_id=20)
    health = NodeHealth(node_id=20)

    for i in range(500):
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=i * 10_000
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
    node = Node(node_id=20)
    health = NodeHealth(node_id=20)
    other_ap = bytes.fromhex("aabbccddeeff")

    for i in range(50):
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi())), capture_us=i * 10_000
        )
        assert health.observe(parse_frame(datagram), now=1000.0 + i * 0.01) is False

    moved = node.on_packet(
        parse_nexmon(nexmon_packet(sample_csi(), src_mac=other_ap)), capture_us=50 * 10_000
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
    node = Node(node_id=20)
    epochs = set()
    for i in range(30):
        datagram = node.on_packet(
            parse_nexmon(nexmon_packet(sample_csi(), seq=i)), capture_us=i * 10_000
        )
        epochs.add(parse_frame(datagram).link_epoch)
    assert epochs == {0}


def test_the_transmitter_reaches_the_frame():
    """src_mac is the one v2 field a nexmon packet answers directly."""
    node = Node(node_id=20)
    frame = parse_frame(node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 0))
    assert frame.src_mac == AP_MAC


def test_a_channel_switch_also_counts_as_a_new_link():
    node = Node(node_id=20)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), chanspec=0x1006)), 0)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), chanspec=0x100B)), 10_000)
    assert node.link_changes == 1


def test_link_stays_the_same_when_nothing_changes():
    node = Node(node_id=20)
    for i in range(20):
        node.on_packet(parse_nexmon(nexmon_packet(sample_csi(), seq=i)), capture_us=i * 10_000)
    assert node.link_changes == 0


def test_rssi_from_the_driver_reaches_the_frame():
    node = Node(node_id=20)
    node.rssi, node.noise = -58, -92
    frame = parse_frame(node.on_packet(parse_nexmon(nexmon_packet(sample_csi())), 0))
    assert frame.rssi == -58
    assert frame.noise_floor == -92


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


def test_timestamp_us_reads_the_kernel_control_message():
    import socket as s

    anc = [(s.SOL_SOCKET, csi_node.SCM_TIMESTAMPNS, struct.pack("qq", 1700, 250_000_000))]
    assert timestamp_us(anc) == 1700 * 1_000_000 + 250_000


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


def test_icmp_echo_checksums_to_zero():
    """A correct checksum is the difference between the AP replying and ignoring us."""
    packet = csi_node.icmp_echo(0x1234, 7)
    assert csi_node.checksum(packet) == 0


def test_timestamp_us_reads_a_32_bit_timespec():
    """32-bit Raspberry Pi OS is the default image for every Pi that is not a 4 or a 5, and
    SO_TIMESTAMPNS is the legacy option, so there the kernel hands back an 8-byte `timespec32`.
    Demanding 16 bytes drops every kernel timestamp on the floor — and the fallback is a
    userspace clock carrying this process's scheduling jitter, which is exactly the thing the
    breathing estimator resamples on and cannot repair."""
    import socket as s

    anc = [(s.SOL_SOCKET, csi_node.SCM_TIMESTAMPNS, struct.pack("=ii", 1700, 250_000_000))]
    assert timestamp_us(anc) == 1700 * 1_000_000 + 250_000


def test_timestamp_us_reads_a_64_bit_timespec():
    import socket as s

    anc = [(s.SOL_SOCKET, csi_node.SCM_TIMESTAMPNS, struct.pack("=qq", 1700, 250_000_000))]
    assert timestamp_us(anc) == 1700 * 1_000_000 + 250_000


def test_timestamp_us_rejects_a_length_it_does_not_recognise():
    import socket as s

    anc = [(s.SOL_SOCKET, csi_node.SCM_TIMESTAMPNS, b"\x00" * 12)]
    assert timestamp_us(anc) is None


# --- a real frame, off real hardware -------------------------------------------------------
#
# Everything else in this file builds packets with the same code that reads them, so a wrong
# header layout agrees with itself and the suite stays green. It did, for the whole life of
# NEXMON_HEADER_SIZE = 16, while the node rejected every genuine frame it ever saw.
#
# These are the first 18 bytes of an actual capture: bcm43455c0, 7.45.189, Pi 4 on Debian 13,
# associated to a 5 GHz AP on channel 40 at 80 MHz. The full payload was 1042 bytes.
REAL_FRAME_HEADER = bytes.fromhex("1111e3807ada88a3034c80ab00002ae16500")
REAL_FRAME_LEN = 1042
REAL_AP_MAC = bytes.fromhex("7ada88a3034c")
REAL_CHANSPEC = 0xE12A  # what `nexutil -Iwlan0 -k` reported for the same link


def test_parses_a_real_capture_header():
    payload = REAL_FRAME_HEADER + bytes(REAL_FRAME_LEN - len(REAL_FRAME_HEADER))
    pkt = parse_nexmon(payload)

    assert pkt is not None, "a genuine nexmon frame must parse"
    # (1042 - 18) / 4 -- an 80 MHz capture on this chip.
    assert pkt.n_sub == 256
    assert pkt.src_mac == REAL_AP_MAC
    assert pkt.chanspec == REAL_CHANSPEC
    assert pkt.core == 0 and pkt.spatial == 0


def test_real_frame_length_is_incompatible_with_a_16_byte_header():
    """The specific arithmetic that made the old layout reject live frames silently.

    1042 - 16 = 1026, and 1026 % 4 == 2, so the int16-pair check failed for every frame.
    """
    assert (REAL_FRAME_LEN - 16) % 4 != 0
    assert (REAL_FRAME_LEN - NEXMON_HEADER_SIZE) % 4 == 0


# -- server control: scan parsing and the poll loop ---------------------------------------

from csi_node import Controller, ControlState, freq_to_channel, parse_iw_scan  # noqa: E402


def test_freq_to_channel_covers_the_bands():
    assert freq_to_channel(2412) == 1
    assert freq_to_channel(2437) == 6
    assert freq_to_channel(2484) == 14
    assert freq_to_channel(5180) == 36
    assert freq_to_channel(5955) == 1  # 6 GHz ch 1
    assert freq_to_channel(3000) == 0  # nothing plausible


IW_SCAN = """\
BSS 7a:da:88:a3:03:4c(on wlan0) -- associated
\tfreq: 5180
\tsignal: -42.00 dBm
\tSSID: asikkala
\tHT operation:
\t\t * primary channel: 36
\t\t * secondary channel offset: above
\tVHT operation:
\t\t * channel width: 1 (80 MHz)
\tBSS Load:
\t\t * station count: 5
\t\t * channel utilisation: 51/255
BSS 00:11:22:33:44:55(on wlan0)
\tfreq: 2437
\tsignal: -70.00 dBm
\tSSID: neighbour
"""


def test_parse_iw_scan_reads_both_bss():
    aps = parse_iw_scan(IW_SCAN)
    assert len(aps) == 2
    # Sorted by signal, so the strong 5 GHz AP is first.
    strong = aps[0]
    assert strong["bssid"] == "7a:da:88:a3:03:4c"
    assert strong["ssid"] == "asikkala"
    assert strong["channel"] == 36
    assert strong["width"] == 80
    assert strong["signal"] == -42.0
    assert strong["stations"] == 5
    assert strong["utilisation"] == 0.2  # 51/255
    assert strong["associated"] is True

    weak = aps[1]
    assert weak["ssid"] == "neighbour"
    assert weak["channel"] == 6
    assert weak["width"] == 20
    assert weak["associated"] is False


def test_parse_iw_scan_tolerates_junk():
    assert parse_iw_scan("") == []
    assert parse_iw_scan("garbage\nno bss here\n") == []
    # A BSS with an unreadable body still appears, with defaults.
    aps = parse_iw_scan("BSS aa:bb:cc:dd:ee:ff(on wlan0)\n\tsomething weird\n")
    assert len(aps) == 1 and aps[0]["width"] == 20 and aps[0]["ssid"] == ""


class FakeController(Controller):
    """A Controller whose HTTP and shell calls are recorded instead of performed."""

    def __init__(self, node, **kwargs):
        self._responses = kwargs.pop("responses", [])
        self._posts = []
        self._commands = []
        state = ControlState("auto")
        super().__init__(
            "http://server:8080", 20, state, node, "wlan0",
            runner=self._fake_run, **kwargs,
        )
        self._get_index = 0

    def _fake_run(self, argv, timeout_s=30.0):
        self._commands.append(argv)
        if argv[1:3] == ["dev", "scan"] or "scan" in argv:
            return IW_SCAN
        return ""

    def _http(self, method, path, payload=None):
        if method == "POST":
            self._posts.append((path, payload))
            return {}
        response = self._responses[min(self._get_index, len(self._responses) - 1)]
        self._get_index += 1
        return response


def _node():
    return Node(20)


def test_controller_applies_stimulus_mode_from_desired():
    ctrl = FakeController(
        _node(),
        responses=[{
            "desired": {"channel": "auto", "stimulus": "off"},
            "revision": 3, "scan_rev": 0,
        }],
    )
    ctrl._poll_once()
    assert ctrl.state.stimulus_mode == "off"
    assert ctrl.applied_rev == 3
    # It reports the applied state back.
    assert ctrl._posts and ctrl._posts[0][1]["applied"]["stimulus"] == "off"


def test_controller_retunes_on_channel_change(monkeypatch):
    monkeypatch.setenv("CSI_TUNE_SH", "/opt/csi-node/bin/tune.sh")
    ctrl = FakeController(
        _node(),
        responses=[{
            "desired": {"channel": "36/80", "stimulus": "auto"},
            "revision": 1, "scan_rev": 0,
        }],
    )
    ctrl._poll_once()
    assert ctrl.applied_channel == "36/80"
    assert ctrl.retunes == 1
    assert ["/opt/csi-node/bin/tune.sh", "36/80"] in ctrl._commands


def test_controller_ignores_unchanged_revision():
    resp = {
        "desired": {"channel": "auto", "stimulus": "always"},
        "revision": 2, "scan_rev": 0,
    }
    ctrl = FakeController(_node(), responses=[resp, resp])
    ctrl._poll_once()
    ctrl._poll_once()
    # Applied once; the second poll saw the same revision and did nothing but report.
    assert ctrl.state.stimulus_mode == "always"
    assert len(ctrl._posts) == 2


def test_controller_scans_when_scan_rev_advances(monkeypatch):
    monkeypatch.setenv("CSI_IW", "/usr/sbin/iw")
    ctrl = FakeController(
        _node(),
        responses=[
            {"desired": {"channel": "auto", "stimulus": "auto"}, "revision": 0, "scan_rev": 0},
            {"desired": {"channel": "auto", "stimulus": "auto"}, "revision": 0, "scan_rev": 1},
        ],
    )
    ctrl._poll_once()  # establishes the scan_rev baseline; no scan owed
    assert ctrl.scans == 0
    ctrl._poll_once()  # scan_rev advanced -> scan
    assert ctrl.scans == 1
    scan_post = [p for _, p in ctrl._posts if p.get("scan") is not None]
    assert scan_post and len(scan_post[0]["scan"]["aps"]) == 2


# -- guard bands, quantizer scaling and the overlong-frame quirk ---------------------------

from csi_node import QUANT_PEAK, data_bins  # noqa: E402


def test_data_bins_excludes_dc_and_guards():
    mask = data_bins(256)
    # k = 0, +/-1 is the DC null; |k| > 122 is guard.
    assert not mask[0]
    assert not mask[1] and not mask[255]
    assert mask[2] and mask[254]
    assert mask[122] and mask[134]  # k = +122 and k = -122
    assert not mask[123] and not mask[133]
    assert mask.sum() == 242


@pytest.mark.parametrize("n_sub,expected", [(64, 52), (128, 114), (256, 242)])
def test_data_bins_counts_per_width(n_sub, expected):
    assert data_bins(n_sub).sum() == expected


def test_quantize_is_not_dominated_by_a_guard_bin():
    """The bug this fixes: one enormous DC bin set the scale and zeroed the real signal."""
    csi = np.full(256, 100.0 + 0j, dtype=np.complex64)
    csi[0] = 34640.0  # DC residue, as measured on hardware

    out = quantize(csi)
    mag = np.hypot(out[1::2].astype(float), out[0::2].astype(float))

    # Every data subcarrier must survive with real resolution, not round to zero.
    assert (mag[data_bins(256)] > 0).all()
    assert np.median(mag[data_bins(256)]) >= QUANT_PEAK / 2


def test_quantize_clips_rather_than_wraps():
    """Guard bins now exceed full scale by design; int8 overflow would wrap them negative."""
    csi = np.full(256, 10.0 + 0j, dtype=np.complex64)
    csi[0] = 1e6  # far beyond full scale once the data bins set the scale

    out = quantize(csi)
    assert out.min() >= -127 and out.max() <= 127
    # The offending bin saturates positive; it must not come back as a small negative number.
    assert out[1] == 127


def test_occupied_span_finds_the_block_the_guards_were_hiding():
    """Guard residue used to dominate the block powers and name the wrong block."""
    csi = np.full(256, 1.0 + 0j, dtype=np.complex64)
    csi[128:192] = 40.0  # real narrowband signal in Q2
    csi[0] = 5000.0      # DC residue in Q0, larger than anything real
    csi[124:132] = 5000.0

    span = occupied_span(csi)
    assert span is not None
    assert span.start == 128 and span.stop == 192


def test_overlong_frame_is_trimmed_to_a_real_width():
    csi = sample_csi(256)
    payload = nexmon_packet(np.append(csi, csi[:1]))  # 257 subcarriers
    pkt = parse_nexmon(payload)

    assert pkt is not None
    assert pkt.n_sub == 256
    np.testing.assert_allclose(pkt.csi, np.rint(csi.real) + 1j * np.rint(csi.imag))


def test_overlong_frame_does_not_bump_the_link_epoch():
    """The churn that reset the server's history twice per stray frame."""
    node = Node(1)
    good = nexmon_packet(sample_csi(256))
    odd = nexmon_packet(np.append(sample_csi(256), 1 + 1j))

    for payload in (good, odd, good, odd, good):
        node.on_packet(parse_nexmon(payload), 1000)

    assert node.epoch == 0
    assert node.link_changes == 0


# -- following the traffic when the node generates none of its own -------------------------

from csi_node import ClassFollower  # noqa: E402


def _settle(f, current=False, narrow=0, wide=0):
    """Prime the follower's first window, which only establishes a baseline."""
    return f.update(0.0, narrow, wide, current)


def test_follower_adopts_narrowband_when_that_is_all_there_is():
    """The 41 Hz -> 1.7 Hz regression: an all-beacon channel with the stimulus off."""
    f = ClassFollower(window_s=5.0, dwell_s=30.0)
    _settle(f)
    # One window later, 200 narrowband frames and nothing wide.
    assert f.update(35.0, 200, 0, False) is True


def test_follower_leaves_a_wideband_channel_alone():
    f = ClassFollower(window_s=5.0, dwell_s=30.0)
    _settle(f)
    assert f.update(35.0, 0, 200, False) is False


def test_follower_ignores_a_narrow_majority():
    """A near-even split is not evidence; switching costs the server its history."""
    f = ClassFollower(window_s=5.0, dwell_s=30.0, margin=2.0)
    _settle(f)
    assert f.update(35.0, 110, 100, False) is False


def test_follower_respects_the_dwell():
    f = ClassFollower(window_s=5.0, dwell_s=30.0)
    _settle(f)
    assert f.update(35.0, 200, 0, False) is True
    # Traffic flips straight back, but the decision has to hold.
    assert f.update(45.0, 200, 400, True) is True
    assert f.changes == 1


def test_follower_says_nothing_on_a_silent_channel():
    f = ClassFollower(window_s=5.0, dwell_s=30.0)
    _settle(f, current=True)
    assert f.update(35.0, 0, 0, True) is True
    assert f.changes == 0


def test_node_counts_both_classes_for_the_follower():
    node = Node(node_id=20)
    node.on_packet(parse_nexmon(nexmon_packet(narrowband_csi(), chanspec=CHANSPEC_CH36_80)), 0)
    node.on_packet(parse_nexmon(nexmon_packet(sample_csi(256), chanspec=CHANSPEC_CH36_80)), 10_000)

    assert node.narrowband_seen == 1
    assert node.wideband_seen == 1


# -- recovering from a firmware that stopped delivering ------------------------------------

from csi_node import CaptureWatchdog  # noqa: E402


def test_watchdog_does_not_fire_while_frames_arrive():
    w = CaptureWatchdog(stall_s=90.0, cooldown_s=120.0)
    assert w.should_rearm(0.0) is False  # starts the clock
    for t in range(10, 400, 10):
        w.note_frame(float(t))
        assert w.should_rearm(float(t)) is False
    assert w.rearms == 0


def test_watchdog_fires_once_the_stall_passes():
    w = CaptureWatchdog(stall_s=90.0, cooldown_s=120.0)
    w.should_rearm(0.0)
    w.note_frame(10.0)
    assert w.should_rearm(80.0) is False   # 70s of silence, not yet
    assert w.should_rearm(150.0) is True   # 140s
    assert w.rearms == 1


def test_watchdog_respects_its_cooldown():
    """A genuinely silent channel must not turn into a re-arm loop."""
    w = CaptureWatchdog(stall_s=90.0, cooldown_s=120.0)
    w.should_rearm(0.0)
    w.note_frame(10.0)
    assert w.should_rearm(150.0) is True
    assert w.should_rearm(250.0) is False  # inside the cooldown
    assert w.should_rearm(400.0) is True
    assert w.rearms == 2


def test_watchdog_waits_a_cooldown_before_its_first_verdict():
    """A slow start is not a stall: nothing has arrived yet because nothing has been asked for."""
    w = CaptureWatchdog(stall_s=10.0, cooldown_s=120.0)
    assert w.should_rearm(0.0) is False
    assert w.should_rearm(60.0) is False


def test_node_and_server_agree_on_which_subcarriers_carry_data():
    """The node keeps its own copy of the active ranges, because it runs on a Pi with no
    server package installed. Two copies of a table is two chances to be wrong, and the way
    this one goes wrong is quiet: the node would scale frames against a set of bins the server
    then masks differently, and nothing would report a mismatch.

    Compared against the server's own layout rather than against a second hardcoded list, so
    editing LAYOUTS is what fails this — which is the point.
    """
    from csi.dsp.subcarriers import valid_mask

    for n_sub in (64, 128, 256):
        node_mask = csi_node.data_bins(n_sub)
        # The server additionally drops pilots; the node deliberately does not, because a pilot
        # still carries transmitted power and belongs in a scale estimate.
        server_mask = valid_mask(n_sub, drop_pilots=False)
        np.testing.assert_array_equal(
            node_mask, server_mask,
            err_msg=f"node and server disagree about the data bins at n_sub={n_sub}",
        )
