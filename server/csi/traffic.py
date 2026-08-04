"""What the capture is actually made of: which transmitters, at what rate, with what holes.

Every row of the waterfall is one 802.11 frame that happened to be on the air. That is the whole
of the sampling story — the analysis rate is not something the server picks, it is whatever the
room's traffic handed over. So when breathing will not converge or the waterfall grows stripes,
the question is almost never "is the DSP wrong" and almost always "what was the air doing", and
until now the only answer available was a cumulative frame count per MAC with no time axis.

This module keeps the time axis. Per node and per source MAC it holds one-second buckets in a
fixed ring, which is what makes "80 Hz" separable from "800 frames in one second and silence
for the next nine" — two captures with the same mean rate, one of which is usable and one of
which is not.

Buckets rather than a deque of arrival times: at 100 Hz a two-minute window is 12000 timestamps
per source, and the report walks every source at 1 Hz. The bucket ring is 120 slots whatever the
rate, it never allocates after construction, and every number the UI shows — rate, duty cycle,
longest gap, RSSI spread, the sparkline itself — is a walk over those slots.

No I/O and no asyncio here: `Hub.handle_datagram` calls `observe` on the event loop (a handful
of integer operations per frame) and the HTTP handler calls `report`. Nothing in this module
touches a ring, so invariant 4 is not in play.
"""

from __future__ import annotations

from collections.abc import Mapping

from .protocol import Frame

# Seconds of per-second history kept, per node and per source. Matches the default in-memory CSI
# history, so the traffic timeline covers the same span as the waterfall it explains.
WINDOW_S = 120

# Distinct source MACs remembered per node. An unfiltered monitor on a busy channel hears a long
# tail of one-off addresses — probe requests, passers-by, a neighbour's phone waking up — and
# past this many, the longest-silent are the right ones to forget.
MAX_SOURCES = 64


def format_mac(mac: bytes) -> str:
    return mac.hex(":")


def is_locally_administered(mac: bytes) -> bool:
    """Bit 1 of the first octet: the address was assigned by software, not by a vendor.

    On a modern phone or laptop this is what MAC randomization looks like from the air. Worth
    surfacing because it explains a source that appears, contributes a burst of frames and never
    comes back under the same name — which otherwise reads as a flapping link.
    """
    return bool(mac[0] & 0x02)


def is_group(mac: bytes) -> bool:
    """Bit 0 of the first octet: a multicast or broadcast address.

    Never legal in a *source* address, so seeing one here does not mean interesting traffic — it
    means the offset this MAC was read from is wrong. Reported rather than hidden, because the
    symptom of a misread header is otherwise a plausible-looking table of transmitters that do
    not exist (see the header-size argument in pi/csi_node.py).
    """
    return bool(mac[0] & 0x01)


class SecondBuckets:
    """One-second counters in a fixed ring, addressed by absolute second.

    Each slot remembers which second it holds. A slot whose second falls outside the window is
    stale by definition and reads as zero, so nothing has to expire anything on a timer — an
    idle source simply stops being counted, and one that goes quiet for an hour costs the same
    as one that never stopped.
    """

    __slots__ = ("size", "_second", "_count", "_rssi_sum", "_rssi_min", "_rssi_max", "_gap_max")

    def __init__(self, size: int = WINDOW_S) -> None:
        self.size = size
        self._second = [-1] * size
        self._count = [0] * size
        self._rssi_sum = [0] * size
        self._rssi_min = [0] * size
        self._rssi_max = [0] * size
        self._gap_max = [0.0] * size

    def add(self, received_at: float, rssi: int, gap_s: float) -> None:
        second = int(received_at)
        slot = second % self.size
        if self._second[slot] != second:
            self._second[slot] = second
            self._count[slot] = 0
            self._rssi_sum[slot] = 0
            self._rssi_min[slot] = rssi
            self._rssi_max[slot] = rssi
            self._gap_max[slot] = 0.0
        self._count[slot] += 1
        self._rssi_sum[slot] += rssi
        if rssi < self._rssi_min[slot]:
            self._rssi_min[slot] = rssi
        if rssi > self._rssi_max[slot]:
            self._rssi_max[slot] = rssi
        if gap_s > self._gap_max[slot]:
            self._gap_max[slot] = gap_s

    def window(self, now: float) -> WindowSummary:
        """Everything derivable from the last `size` seconds, in one pass."""
        newest = int(now)
        oldest = newest - self.size + 1
        counts = [0] * self.size
        frames = 0
        rssi_sum = 0
        rssi_min: int | None = None
        rssi_max: int | None = None
        gap_max = 0.0
        active = 0

        for slot in range(self.size):
            second = self._second[slot]
            if second < oldest or second > newest:
                continue
            count = self._count[slot]
            if count == 0:
                continue
            counts[second - oldest] = count
            frames += count
            active += 1
            rssi_sum += self._rssi_sum[slot]
            low, high = self._rssi_min[slot], self._rssi_max[slot]
            rssi_min = low if rssi_min is None or low < rssi_min else rssi_min
            rssi_max = high if rssi_max is None or high > rssi_max else rssi_max
            if self._gap_max[slot] > gap_max:
                gap_max = self._gap_max[slot]

        return WindowSummary(counts, frames, rssi_sum, rssi_min, rssi_max, gap_max, active)


class WindowSummary:
    """One `SecondBuckets` read, kept as an object so callers do not unpack a seven-tuple."""

    __slots__ = ("counts", "frames", "rssi_sum", "rssi_min", "rssi_max", "gap_max", "active_s")

    def __init__(
        self,
        counts: list[int],
        frames: int,
        rssi_sum: int,
        rssi_min: int | None,
        rssi_max: int | None,
        gap_max: float,
        active_s: int,
    ) -> None:
        self.counts = counts
        self.frames = frames
        self.rssi_sum = rssi_sum
        self.rssi_min = rssi_min
        self.rssi_max = rssi_max
        self.gap_max = gap_max
        self.active_s = active_s

    @property
    def rssi_mean(self) -> float | None:
        return self.rssi_sum / self.frames if self.frames else None


class Source:
    """One transmitter as heard by one node."""

    __slots__ = (
        "mac",
        "frames",
        "first_seen",
        "last_seen",
        "rssi",
        "channel",
        "n_sub",
        "buckets",
        "_last_arrival",
    )

    def __init__(self, mac: bytes, received_at: float, window_s: int) -> None:
        self.mac = mac
        self.frames = 0
        self.first_seen = received_at
        self.last_seen = received_at
        self.rssi = 0
        self.channel = 0
        self.n_sub = 0
        self.buckets = SecondBuckets(window_s)
        self._last_arrival = 0.0

    def observe(self, frame: Frame, received_at: float) -> None:
        gap = received_at - self._last_arrival if self._last_arrival else 0.0
        self._last_arrival = received_at
        self.last_seen = received_at
        self.frames += 1
        self.rssi = frame.rssi
        self.channel = frame.channel
        self.n_sub = frame.n_sub
        self.buckets.add(received_at, frame.rssi, gap)


class TrafficTracker:
    """The traffic picture for one node.

    Not cleared on a roam or a reboot, unlike everything else the hub keeps per node. Those
    events clear history because a baseline measured on the old link describes a different room;
    this measures the air rather than the room, and a re-association is precisely the thing you
    open this view to see — the old BSSID going quiet at the same second the new one starts.
    """

    def __init__(self, window_s: int = WINDOW_S, max_sources: int = MAX_SOURCES) -> None:
        self.window_s = window_s
        self.max_sources = max_sources
        self.sources: dict[bytes, Source] = {}
        self.buckets = SecondBuckets(window_s)
        self.frames = 0
        # Frames whose source MAC is six zero bytes: a v1 uplink, which has no such field. They
        # count towards the node's rate — they are real captured frames — but they cannot be
        # attributed, and pretending 00:00:00:00:00:00 is a transmitter would put a phantom row
        # at the top of the table for every ESP32 running older firmware.
        self.anonymous = 0
        self.evicted = 0
        self.first_seen = 0.0
        self.last_seen = 0.0
        self._last_arrival = 0.0

    def observe(self, frame: Frame, received_at: float) -> None:
        if not self.first_seen:
            self.first_seen = received_at
        gap = received_at - self._last_arrival if self._last_arrival else 0.0
        self._last_arrival = received_at
        self.last_seen = received_at
        self.frames += 1
        self.buckets.add(received_at, frame.rssi, gap)

        mac = frame.src_mac
        if not any(mac):
            self.anonymous += 1
            return

        source = self.sources.get(mac)
        if source is None:
            if len(self.sources) >= self.max_sources:
                quietest = min(self.sources, key=lambda key: self.sources[key].last_seen)
                del self.sources[quietest]
                self.evicted += 1
            source = self.sources[mac] = Source(mac, received_at, self.window_s)
        source.observe(frame, received_at)

    # -- reporting -------------------------------------------------------------------------

    def transmitters(self) -> list[dict]:
        """The WiFi overview's shape: cumulative totals, loudest first.

        Kept alongside `report` rather than derived from it because that page wants the long
        view — everything this node has ever heard — while the traffic view wants the window.
        """
        return [
            {
                "mac": format_mac(source.mac),
                "frames": source.frames,
                "rssi": source.rssi,
                "channel": source.channel,
                "last_seen": source.last_seen,
            }
            for source in sorted(self.sources.values(), key=lambda s: -s.frames)
        ]

    def report(self, now: float, aps: Mapping[str, str] | None = None) -> dict:
        """The full picture for one node over the last `window_s` seconds.

        `aps` maps BSSID to SSID, from the node's last scan. It is what lets a row be labelled
        as an access point rather than left as an anonymous MAC, and it is passed in rather than
        looked up here so this module stays free of the control store.
        """
        node = self.buckets.window(now)
        covered = self._covered_seconds(now)
        span_s = float(max(covered, 1))

        sources = [
            self._source_report(source, now, node.frames, aps or {})
            for source in self.sources.values()
        ]
        # Loudest in the window first, then by recency — so a source that has gone silent sinks
        # rather than holding a top row on the strength of what it did two minutes ago.
        sources.sort(key=lambda row: (-row["frames_window"], -row["last_seen"]))

        return {
            "node_id": None,  # filled in by the caller, which is the one that knows
            "window_s": self.window_s,
            "covered_s": covered,
            "counts": node.counts,
            "frames": self.frames,
            "frames_window": node.frames,
            "rate_hz": node.frames / span_s,
            # Fraction of the covered seconds that carried at least one frame. This is the
            # number that separates a slow capture from an intermittent one: 40 Hz at duty 1.0
            # draws an even waterfall, 40 Hz at duty 0.5 draws it in stripes.
            "duty": node.active_s / covered if covered else 0.0,
            "silent_s": max(0, covered - node.active_s),
            "max_gap_s": node.gap_max,
            "anonymous": self.anonymous,
            "evicted": self.evicted,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "sources": sources,
        }

    def _covered_seconds(self, now: float) -> int:
        """How much of the window this node has actually been around for.

        A node that appeared three seconds ago has not been silent for the other 117, and
        dividing by the full window would report it at a fortieth of its real rate.
        """
        if not self.first_seen:
            return 0
        seen = int(now) - int(self.first_seen) + 1
        return max(1, min(self.window_s, seen))

    def _source_report(
        self, source: Source, now: float, node_frames: int, aps: Mapping[str, str]
    ) -> dict:
        window = source.buckets.window(now)
        covered = max(1, min(self.window_s, int(now) - int(source.first_seen) + 1))
        mac = format_mac(source.mac)
        ssid = aps.get(mac)
        return {
            "mac": mac,
            "frames": source.frames,
            "frames_window": window.frames,
            "rate_hz": window.frames / covered,
            "share": window.frames / node_frames if node_frames else 0.0,
            "counts": window.counts,
            "rssi": source.rssi,
            "rssi_mean": window.rssi_mean,
            "rssi_min": window.rssi_min,
            "rssi_max": window.rssi_max,
            "channel": source.channel,
            "n_sub": source.n_sub,
            "max_gap_s": window.gap_max,
            "duty": window.active_s / covered,
            "first_seen": source.first_seen,
            "last_seen": source.last_seen,
            # An access point if the node's own scan saw this BSSID. Anything else is a station
            # — or rather, is not known to be an access point, which is a weaker claim and the
            # only honest one available from a source address alone.
            "ap": ssid is not None,
            "ssid": ssid or "",
            "randomized": is_locally_administered(source.mac),
            "malformed": is_group(source.mac),
        }
