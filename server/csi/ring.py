"""Rolling in-memory history of preprocessed frames, one ring per node.

Everything downstream of preprocessing — presence, breathing, the placement tuner, subcarrier
ranking — works on a time window rather than a single frame, and they all want overlapping
windows of the same data. One ring per node, read by many, is cheaper and simpler than each
analyzer keeping its own buffer.

Storage is a preallocated (capacity, n_sub) float32 matrix written in place. No per-frame
allocation, no growth, no GC pressure at 100 Hz.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .dsp.preprocess import Processed


class History(Protocol):
    """What an analyzer needs from the past. Satisfied by `FrameRing` and by `Window`.

    Both implement it so that a snapshot taken off the ring can stand in for the ring itself.
    The DSP runs in a worker thread and must not touch a buffer the ingest task is writing;
    because it only ever reaches for history through this interface, handing it a frozen copy
    is a substitution it cannot observe.
    """

    n_sub: int

    def __len__(self) -> int: ...

    def seconds(self, duration_s: float) -> Window: ...


@dataclass(frozen=True, slots=True)
class Window:
    """A contiguous slice of history, oldest first.

    `amp` retains NaN at masked subcarriers. Analyzers select columns via `mask` rather than
    dropping them, so a column index means the same subcarrier everywhere in the system.
    """

    t_us: np.ndarray  # int64, shape (n,)
    amp: np.ndarray  # float32, shape (n, n_sub)
    agc: np.ndarray  # bool, shape (n,) — True where an AGC step was detected
    rssi: np.ndarray  # int8, shape (n,)
    mask: np.ndarray  # bool, shape (n_sub,)

    def __len__(self) -> int:
        return int(self.t_us.size)

    @property
    def n_sub(self) -> int:
        return int(self.mask.size)

    @property
    def duration_s(self) -> float:
        if len(self) < 2:
            return 0.0
        return float(self.t_us[-1] - self.t_us[0]) / 1e6

    @property
    def rate_hz(self) -> float:
        """Sample rate measured from device timestamps, not from arrival times."""
        d = self.duration_s
        return (len(self) - 1) / d if d > 0 else 0.0

    def columns(self, indices: np.ndarray) -> np.ndarray:
        """Amplitude for a subset of subcarriers, shape (n, len(indices))."""
        return self.amp[:, indices]

    def seconds(self, duration_s: float) -> Window:
        """The newest frames spanning at most `duration_s`, by device timestamp.

        Same contract as `FrameRing.seconds`, selecting on the same clock — that equivalence is
        what lets a snapshot substitute for the ring. Slices rather than copies, which is safe
        precisely because a snapshot's arrays are already nobody else's.
        """
        if self.t_us.size == 0:
            return self
        cutoff = int(self.t_us[-1]) - int(duration_s * 1e6)
        keep = int(np.searchsorted(self.t_us, cutoff, side="left"))
        if keep == 0:
            return self
        return Window(
            t_us=self.t_us[keep:],
            amp=self.amp[keep:],
            agc=self.agc[keep:],
            rssi=self.rssi[keep:],
            mask=self.mask,
        )


class FrameRing:
    """Fixed-capacity ring of preprocessed frames for one node."""

    def __init__(self, n_sub: int, capacity: int) -> None:
        if capacity < 2:
            raise ValueError("capacity must be at least 2")
        self.n_sub = n_sub
        self.capacity = capacity
        self._amp = np.full((capacity, n_sub), np.nan, dtype=np.float32)
        self._t = np.zeros(capacity, dtype=np.int64)
        self._agc = np.zeros(capacity, dtype=bool)
        self._rssi = np.zeros(capacity, dtype=np.int8)
        self._mask = np.zeros(n_sub, dtype=bool)
        self._write = 0
        self._count = 0
        # Monotonic counter of everything ever pushed. Consumers keep the value they last saw
        # to ask "what is new since then" without holding a reference to the data.
        self.total = 0

    def __len__(self) -> int:
        return self._count

    @property
    def mask(self) -> np.ndarray:
        return self._mask

    def push(self, processed: Processed) -> None:
        if processed.frame.n_sub != self.n_sub:
            raise ValueError(
                f"n_sub changed {self.n_sub} -> {processed.frame.n_sub}; the caller must "
                "build a new ring rather than mixing bandwidths in one history"
            )
        i = self._write
        self._amp[i] = processed.amp
        self._t[i] = processed.frame.timestamp
        self._agc[i] = processed.agc_step
        self._rssi[i] = processed.frame.rssi
        self._mask = processed.mask
        self._write = (i + 1) % self.capacity
        self._count = min(self._count + 1, self.capacity)
        self.total += 1

    def clear(self) -> None:
        self._write = 0
        self._count = 0
        self._amp.fill(np.nan)

    def _order(self, n: int) -> np.ndarray:
        """Indices of the newest `n` entries, oldest first."""
        n = min(n, self._count)
        start = (self._write - n) % self.capacity
        return (start + np.arange(n)) % self.capacity

    def latest(self, n: int) -> Window:
        """The newest `n` frames, oldest first. Returns fewer if history is short."""
        idx = self._order(n)
        return Window(
            t_us=self._t[idx],
            amp=self._amp[idx],
            agc=self._agc[idx],
            rssi=self._rssi[idx],
            mask=self._mask,
        )

    def seconds(self, duration_s: float) -> Window:
        """The newest frames spanning at most `duration_s`, by device timestamp.

        Selecting on timestamps rather than a frame count means a window is the duration it
        claims to be even when the node has been dropping frames — which matters, because a
        20 s breathing window that is silently 14 s of data produces a confident wrong BPM.
        """
        if self._count == 0:
            return self.latest(0)
        idx = self._order(self._count)
        t = self._t[idx]
        cutoff = t[-1] - int(duration_s * 1e6)
        keep = np.searchsorted(t, cutoff, side="left")
        idx = idx[keep:]
        return Window(
            t_us=self._t[idx],
            amp=self._amp[idx],
            agc=self._agc[idx],
            rssi=self._rssi[idx],
            mask=self._mask,
        )
