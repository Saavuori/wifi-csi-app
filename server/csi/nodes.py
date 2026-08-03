"""Per-node health: sample rate, sequence gaps, RSSI, uptime.

These are the numbers the Phase 1 exit criteria are written against — "stable ~80 Hz",
"sequence gaps under 1% over a 10-minute run" — so they are measured continuously rather than
being something you go and check with a script afterwards.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .protocol import Frame

SEQ_MODULUS = 1 << 32
# UDP can deliver out of order, but not by thousands of frames. A sequence number that has gone
# backwards by less than this, with the device clock still moving forward, is a reorder.
REORDER_TOLERANCE = 1024
# `esp_timer_get_time()` counts from boot, so a device clock that jumps backwards by more than a
# second is a reboot. This is checked *before* the sequence number, because a reboot restarts
# `seq` at zero — which, read as an unsigned delta, looks exactly like a small backwards step
# and would otherwise be filed as a harmless reorder.
REBOOT_CLOCK_MARGIN_US = 1_000_000
# A forward jump larger than this is not a plausible loss burst either.
MAX_PLAUSIBLE_GAP = 100_000


@dataclass(slots=True)
class NodeHealth:
    node_id: int
    first_seen: float = field(default_factory=time.time)
    last_seen: float = 0.0

    frames: int = 0
    expected: int = 0  # frames the sequence numbers say should have arrived
    gaps: int = 0  # number of discontinuities, not number of missing frames
    missing: int = 0
    reorders: int = 0
    reboots: int = 0
    roams: int = 0  # link epoch changes: the node re-associated, possibly to a different AP
    bad_packets: int = 0

    last_seq: int | None = None
    last_timestamp: int = 0
    rssi: int = 0
    noise_floor: int = 0
    channel: int = 0
    n_sub: int = 0
    src_mac: bytes = b"\x00" * 6
    link_epoch: int = 0

    # Recent (device timestamp) intervals, for a rate estimate that does not depend on the
    # server's clock or on the network.
    _intervals: deque = field(default_factory=lambda: deque(maxlen=256), repr=False)

    def observe(self, frame: Frame, *, now: float | None = None) -> bool:
        """Fold in one frame. Returns True if the node's history must be thrown away.

        Two things force that. A reboot resets both the sequence counter and
        `esp_timer_get_time()`, so a window carried across it puts a step change into every
        analysis at once. A link epoch change means the node re-associated — on a mesh network,
        possibly to a different access point — which moves the transmitting antenna and makes
        every baseline built from the old link wrong, with nothing in the amplitudes to say so.
        The second is the more insidious of the two, because unlike a reboot it leaves the
        sequence numbers perfectly continuous.
        """
        self.last_seen = now if now is not None else time.time()
        self.frames += 1
        self.rssi = frame.rssi
        self.noise_floor = frame.noise_floor
        self.channel = frame.channel
        self.n_sub = frame.n_sub
        self.src_mac = frame.src_mac

        roamed = self.frames > 1 and frame.link_epoch != self.link_epoch
        if roamed:
            self.roams += 1
        self.link_epoch = frame.link_epoch

        rebooted = False
        prev = self.last_seq
        if prev is not None:
            delta = (frame.seq - prev) % SEQ_MODULUS
            clock_went_back = frame.timestamp + REBOOT_CLOCK_MARGIN_US < self.last_timestamp

            if clock_went_back or delta > MAX_PLAUSIBLE_GAP:
                rebooted = True
                self.reboots += 1
            elif delta == 0:
                # Duplicate. A reorder, not a gap: retransmission at the network level is not a
                # capture problem and must not show up in the loss rate. Still reports a roam if
                # this was the frame that carried the new epoch — the alternative is to swallow
                # it here and never see it again, because the epoch has already been recorded.
                self.reorders += 1
                return roamed
            elif delta > SEQ_MODULUS - REORDER_TOLERANCE:
                self.reorders += 1
                return roamed
            else:
                self.expected += delta
                if delta > 1:
                    self.gaps += 1
                    self.missing += delta - 1
        else:
            self.expected += 1

        if not rebooted and not roamed and self.last_timestamp:
            if frame.timestamp > self.last_timestamp:
                self._intervals.append(frame.timestamp - self.last_timestamp)

        self.last_seq = frame.seq
        self.last_timestamp = frame.timestamp

        if rebooted:
            self._intervals.clear()
            self.expected += 1
        if roamed:
            # The interval spanning a re-association is the length of the outage, not a sample
            # period. Left in, one roam drags the reported rate down for the next 256 frames.
            self._intervals.clear()
        return rebooted or roamed

    @property
    def rate_hz(self) -> float:
        if not self._intervals:
            return 0.0
        mean_us = sum(self._intervals) / len(self._intervals)
        return 1e6 / mean_us if mean_us > 0 else 0.0

    @property
    def jitter_ms(self) -> float:
        """Standard deviation of the inter-frame interval. High jitter with a good mean rate
        means the sender is being blocked — usually the packer task contending with the WiFi
        driver on Core 0."""
        n = len(self._intervals)
        if n < 2:
            return 0.0
        mean = sum(self._intervals) / n
        var = sum((i - mean) ** 2 for i in self._intervals) / n
        return (var**0.5) / 1000.0

    @property
    def loss_rate(self) -> float:
        return self.missing / self.expected if self.expected else 0.0

    @property
    def uptime_s(self) -> float:
        return self.last_timestamp / 1e6

    def online(self, timeout_s: float, *, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.last_seen) < timeout_s

    def as_dict(self, timeout_s: float = 5.0, *, now: float | None = None) -> dict:
        return {
            "node_id": self.node_id,
            "online": self.online(timeout_s, now=now),
            "rate_hz": round(self.rate_hz, 2),
            "jitter_ms": round(self.jitter_ms, 2),
            "frames": self.frames,
            "missing": self.missing,
            "gaps": self.gaps,
            "loss_rate": round(self.loss_rate, 5),
            "reorders": self.reorders,
            "reboots": self.reboots,
            "roams": self.roams,
            "src_mac": self.src_mac.hex(":") if any(self.src_mac) else "",
            "link_epoch": self.link_epoch,
            "bad_packets": self.bad_packets,
            "rssi": self.rssi,
            "noise_floor": self.noise_floor,
            # None, not a number, when the driver reports no noise floor. `brcmfmac` on the Pi
            # leaves the column empty and the node normalizes that to 0, so subtracting it
            # produced an "SNR" that was RSSI with the sign flipped — -49 dB on a link that was
            # working perfectly. A gap the UI can render as "—" is honest; a plausible-looking
            # wrong number invites someone to tune against it.
            "snr_db": self.rssi - self.noise_floor if self.noise_floor else None,
            "channel": self.channel,
            "n_sub": self.n_sub,
            "uptime_s": round(self.uptime_s, 1),
            "last_seen": self.last_seen,
        }
