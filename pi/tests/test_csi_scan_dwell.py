"""Tests for the per-channel listener behind the monitor-mode sweep.

csi-scan-monitor.sh owns the arming and merges the channels with sort(1); what is worth pinning
down without a Pi is the tally underneath it — who was heard, on which channel, and how loud.
The packets are built by the same helper the node's own tests use, so the header this reads is
the header the node reads.
"""

from __future__ import annotations

import csi_scan_dwell
import numpy as np
from csi_node import parse_nexmon
from test_csi_node import CHANSPEC_CH6, nexmon_packet

CSI = np.ones(64, dtype=np.complex64)
LAPTOP = bytes.fromhex("aabbccddeeff")

# 2.4 GHz channel 11, 20 MHz.
CHANSPEC_CH11 = 0x100B


def tally(*payloads: bytes, expect_channel: int = 0):
    seen: dict = {}
    for payload in payloads:
        pkt = parse_nexmon(payload)
        assert pkt is not None
        csi_scan_dwell.record(seen, pkt, expect_channel)
    return csi_scan_dwell.rows(seen)


def test_counts_frames_and_medians_the_rssi():
    rows = tally(
        nexmon_packet(CSI, rssi=-60),
        nexmon_packet(CSI, rssi=-50),
        nexmon_packet(CSI, rssi=-40),
    )
    assert rows == [("00:11:22:33:44:55", 6, 3, -50)]


def test_one_row_per_transmitter_and_channel():
    """A transmitter heard on two channels is two rows; the shell picks between them.

    Which is the point of keeping them apart: a strong radio bleeds into the neighbouring
    channels, and the chanspec in the packet is the receiver's, so every dwell that hears it
    claims it. Merging here would average a channel it is on with one it is not.
    """
    rows = tally(
        nexmon_packet(CSI, rssi=-40),
        nexmon_packet(CSI, rssi=-70, chanspec=CHANSPEC_CH11),
        nexmon_packet(CSI, rssi=-55, src_mac=LAPTOP),
    )
    assert rows == [
        ("00:11:22:33:44:55", 6, 1, -40),
        ("00:11:22:33:44:55", 11, 1, -70),
        ("aa:bb:cc:dd:ee:ff", 6, 1, -55),
    ]


def test_busiest_transmitter_first():
    rows = tally(
        nexmon_packet(CSI, rssi=-80, src_mac=LAPTOP),
        nexmon_packet(CSI, rssi=-40),
        nexmon_packet(CSI, rssi=-40),
    )
    assert [row[0] for row in rows] == ["00:11:22:33:44:55", "aa:bb:cc:dd:ee:ff"]


def test_unreadable_chanspec_falls_back_to_the_armed_channel():
    """A chanspec that decodes to no channel at all is the one case for taking our own word.

    Everywhere else the packet's channel wins even when it disagrees with what was armed: if the
    retune did not take, the frames really did arrive somewhere else and the sweep should say so.
    """
    rows = tally(nexmon_packet(CSI, rssi=-45, chanspec=0x0000), expect_channel=9)
    assert rows == [("00:11:22:33:44:55", 9, 1, -45)]


def test_a_transmitter_with_no_frames_is_not_invented():
    assert csi_scan_dwell.rows({}) == []


def test_channel_6_helper_matches_the_node():
    """The two test modules share a chanspec constant; if that drifts the rest is meaningless."""
    assert CHANSPEC_CH6 & 0xFF == 6
