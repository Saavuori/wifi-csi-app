"""Per-node traffic accounting: rates, duty, gaps and who the frames came from."""

from __future__ import annotations

import numpy as np
import pytest

from csi.protocol import Frame
from csi.traffic import TrafficTracker, is_group, is_locally_administered

# Real-looking, globally administered addresses: the Raspberry Pi Foundation OUI. Picked over
# the obvious aa:bb:cc:… because 0xaa has the locally-administered bit set, which would make
# every one of these read as a randomized MAC.
AP = bytes.fromhex("b827eb000001")
STATION = bytes.fromhex("b827eb000002")
RANDOM_MAC = bytes.fromhex("0233445566aa")  # bit 1 set: locally administered


def frame(mac: bytes = AP, *, rssi: int = -50, channel: int = 36, n_sub: int = 64) -> Frame:
    return Frame(
        node_id=1,
        seq=0,
        timestamp=0,
        rssi=rssi,
        noise_floor=-92,
        channel=channel,
        sec_channel=0,
        n_sub=n_sub,
        data=np.zeros(2 * n_sub, dtype=np.int8),
        src_mac=mac,
    )


def feed(tracker: TrafficTracker, mac: bytes, *, start: float, count: int, rate_hz: float):
    """`count` frames from one transmitter at a steady rate, starting at `start`."""
    for i in range(count):
        tracker.observe(frame(mac), start + i / rate_hz)


def test_a_steady_source_reports_its_rate():
    tracker = TrafficTracker()
    feed(tracker, AP, start=1000.0, count=800, rate_hz=80.0)

    report = tracker.report(1010.0)
    assert report["frames_window"] == 800
    assert report["rate_hz"] == pytest.approx(80.0, rel=0.15)
    assert report["sources"][0]["mac"] == "b8:27:eb:00:00:01"
    assert report["sources"][0]["rate_hz"] == pytest.approx(80.0, rel=0.15)
    assert report["sources"][0]["share"] == 1.0


def test_share_splits_between_transmitters():
    tracker = TrafficTracker()
    feed(tracker, AP, start=1000.0, count=60, rate_hz=20.0)
    feed(tracker, STATION, start=1000.0, count=20, rate_hz=20.0)

    report = tracker.report(1004.0)
    shares = {row["mac"]: row["share"] for row in report["sources"]}
    assert shares["b8:27:eb:00:00:01"] == pytest.approx(0.75)
    assert shares["b8:27:eb:00:00:02"] == pytest.approx(0.25)
    # Loudest first: the ordering is what makes the table readable without sorting it.
    assert report["sources"][0]["mac"] == "b8:27:eb:00:00:01"


def test_the_window_forgets():
    """The whole point of the bucket ring: two minutes ago is not part of "now"."""
    tracker = TrafficTracker()
    feed(tracker, AP, start=1000.0, count=500, rate_hz=50.0)

    later = tracker.report(1000.0 + tracker.window_s + 30)
    assert later["frames_window"] == 0
    assert later["frames"] == 500  # the cumulative total is not a window
    assert later["sources"][0]["frames_window"] == 0
    assert later["rate_hz"] == 0.0


def test_duty_separates_a_slow_capture_from_an_intermittent_one():
    """Same mean rate, opposite verdicts — which is the reason duty is reported at all."""
    steady = TrafficTracker()
    feed(steady, AP, start=1000.0, count=100, rate_hz=10.0)

    bursty = TrafficTracker()
    for second in range(0, 10, 2):
        feed(bursty, AP, start=1000.0 + second, count=20, rate_hz=100.0)

    steady_report = steady.report(1010.0)
    bursty_report = bursty.report(1010.0)

    assert steady_report["frames_window"] == bursty_report["frames_window"] == 100
    assert steady_report["duty"] == pytest.approx(1.0, abs=0.15)
    assert bursty_report["duty"] < 0.7
    assert bursty_report["silent_s"] >= 4
    assert bursty_report["max_gap_s"] > 1.0


def test_the_rate_of_a_node_that_just_appeared_is_not_divided_by_the_whole_window():
    tracker = TrafficTracker()
    feed(tracker, AP, start=1000.0, count=160, rate_hz=80.0)

    report = tracker.report(1002.0)
    assert report["covered_s"] == 3
    assert report["rate_hz"] > 50.0


def test_counts_are_a_timeline_the_client_can_draw():
    tracker = TrafficTracker()
    feed(tracker, AP, start=1000.0, count=30, rate_hz=10.0)

    report = tracker.report(1005.0)
    counts = report["counts"]
    assert len(counts) == tracker.window_s
    # Newest second last, and only the seconds that carried frames are non-zero.
    assert sum(counts) == 30
    assert counts[-3:] == [0, 0, 0]
    assert counts[-6:-3] == [10, 10, 10]


def test_rssi_is_summarised_over_the_window():
    tracker = TrafficTracker()
    for i, rssi in enumerate((-40, -50, -60)):
        tracker.observe(frame(AP, rssi=rssi), 1000.0 + i)

    row = tracker.report(1003.0)["sources"][0]
    assert row["rssi"] == -60  # last
    assert row["rssi_mean"] == pytest.approx(-50.0)
    assert row["rssi_min"] == -60
    assert row["rssi_max"] == -40


def test_a_v1_frame_is_counted_but_not_attributed():
    """Six zero bytes is "this uplink version has no such field", not a transmitter."""
    tracker = TrafficTracker()
    tracker.observe(frame(b"\x00" * 6), 1000.0)

    report = tracker.report(1000.0)
    assert report["frames_window"] == 1
    assert report["anonymous"] == 1
    assert report["sources"] == []


def test_the_quietest_source_is_evicted_first():
    tracker = TrafficTracker(max_sources=4)
    for i in range(4):
        tracker.observe(frame(bytes([0x10, 0, 0, 0, 0, i])), 1000.0 + i)
    # Node 0 has been silent longest, so the fifth transmitter takes its slot.
    tracker.observe(frame(bytes([0x10, 0, 0, 0, 0, 9])), 1010.0)

    macs = {row["mac"] for row in tracker.report(1010.0)["sources"]}
    assert len(macs) == 4
    assert "10:00:00:00:00:00" not in macs
    assert "10:00:00:00:00:09" in macs
    assert tracker.report(1010.0)["evicted"] == 1


def test_a_scanned_bssid_names_the_source():
    tracker = TrafficTracker()
    tracker.observe(frame(AP), 1000.0)
    tracker.observe(frame(STATION), 1000.0)

    rows = tracker.report(1000.0, aps={"b8:27:eb:00:00:01": "kitchen"})["sources"]
    by_mac = {row["mac"]: row for row in rows}
    assert by_mac["b8:27:eb:00:00:01"]["ap"] is True
    assert by_mac["b8:27:eb:00:00:01"]["ssid"] == "kitchen"
    # Not known to be an access point is all that can be claimed without a scan hit.
    assert by_mac["b8:27:eb:00:00:02"]["ap"] is False


def test_a_randomized_mac_is_flagged():
    tracker = TrafficTracker()
    tracker.observe(frame(RANDOM_MAC), 1000.0)
    tracker.observe(frame(AP), 1000.0)

    rows = {row["mac"]: row for row in tracker.report(1000.0)["sources"]}
    assert rows["02:33:44:55:66:aa"]["randomized"] is True
    assert rows["b8:27:eb:00:00:01"]["randomized"] is False


def test_mac_bits():
    assert is_locally_administered(RANDOM_MAC)
    assert not is_locally_administered(AP)
    # A group bit in a source address cannot happen on the air; it means the header offset is
    # wrong, which is a fault worth naming rather than a transmitter worth listing.
    assert is_group(b"\xff\xff\xff\xff\xff\xff")
    assert not is_group(AP)


def test_the_transmitter_list_keeps_the_wifi_overview_shape():
    tracker = TrafficTracker()
    feed(tracker, STATION, start=1000.0, count=5, rate_hz=10.0)
    feed(tracker, AP, start=1000.0, count=50, rate_hz=10.0)

    rows = tracker.transmitters()
    assert [row["mac"] for row in rows] == ["b8:27:eb:00:00:01", "b8:27:eb:00:00:02"]
    assert rows[0]["frames"] == 50
    assert set(rows[0]) == {"mac", "frames", "rssi", "channel", "last_seen"}
