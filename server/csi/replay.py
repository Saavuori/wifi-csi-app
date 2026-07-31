"""Pump a recording back through the live pipeline.

The replayer's only job is to hand datagrams to the same sink the UDP listener uses, with the
original spacing scaled by a speed factor. It does not parse, normalize, or interpret anything
— every downstream stage sees exactly what it would have seen live, which is what makes
developing against a recording legitimate rather than an approximation.

Timing follows the *device* timestamps in the frames, not arrival order or any wall-clock
metadata. Those are the only jitter-free clock in the system.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import struct
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

from .protocol import HEADER_SIZE_V1, REC_MAGIC, ProtocolError
from .recorder import read_index

Sink = Callable[[bytes, float], Awaitable[None] | None]

# Frames due within this much of now are sent without sleeping. Below a couple of milliseconds
# the event loop cannot honour the delay anyway, so asking it to is pure overhead — and at 10x
# speed almost every frame falls into this bucket, which is how batching happens for free.
_SLEEP_FLOOR_S = 0.002

_LEN = struct.Struct("<H")
_TIMESTAMP_AT = 8  # byte offset of the u64 device timestamp in the uplink header


class Replayer:
    """Replays one recording. The hub allows a single instance at a time."""

    def __init__(
        self,
        path: Path,
        sink: Sink,
        *,
        speed: float = 1.0,
        loop: bool = False,
        on_state: Callable[[dict], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.sink = sink
        self.speed = speed
        self.loop = loop
        self.on_state = on_state

        self.playing = True
        self.finished = False
        self.position_us = 0
        self.first_t_us: int | None = None
        self.last_t_us: int | None = None
        self.frames_sent = 0

        self._index = read_index(self.path)
        self._stamps = [entry[0] for entry in self._index]
        self._seek_to: int | None = None
        self._stop = False
        # One doorbell, rung by `resume`, `seek` and `stop`, for both of the places the pump
        # waits: parked while paused, and waiting for a frame to fall due. Purely a wakeup, never
        # the pause state itself — `playing` is that. Keeping the two apart is what stops a seek
        # from resuming a paused replay; see `_await_resume` and `_sleep_until_due`.
        self._gate = asyncio.Event()

    # -- controls ------------------------------------------------------------------------

    def pause(self) -> None:
        self.playing = False
        self._emit()

    def resume(self) -> None:
        self.playing = True
        self._gate.set()
        self._emit()

    def set_speed(self, speed: float) -> None:
        # 0 means "as fast as the machine can" — for re-analysing an overnight recording in a
        # minute rather than in eight hours.
        self.speed = max(0.0, float(speed))
        self._emit()

    def seek(self, t_us: int) -> None:
        # Wakes the pump so it can restart the file at the new offset, but deliberately does not
        # touch `playing`: scrubbing the timeline of a paused replay leaves it paused.
        self._seek_to = int(t_us)
        self._gate.set()

    def stop(self) -> None:
        self._stop = True
        self._gate.set()

    def state(self) -> dict:
        return {
            "path": self.path.name,
            "playing": self.playing and not self.finished,
            "finished": self.finished,
            "speed": self.speed,
            "loop": self.loop,
            "position_us": self.position_us,
            "first_t_us": self.first_t_us,
            "last_t_us": self.last_t_us,
            "frames_sent": self.frames_sent,
        }

    def _emit(self) -> None:
        if self.on_state:
            self.on_state(self.state())

    # -- the pump ------------------------------------------------------------------------

    async def run(self) -> None:
        try:
            while not self._stop:
                await self._play_once()
                if self._stop:
                    break
                if self._seek_to is not None:
                    continue  # a seek arrived mid-pass; restart at the new offset
                if not self.loop:
                    break
                self.position_us = 0
        finally:
            self.finished = True
            self.playing = False
            self._emit()

    async def _play_once(self) -> None:
        target_seek = self._seek_to
        self._seek_to = None
        start_offset = self._offset_for(target_seek) if target_seek is not None else 0

        with open(self.path, "rb") as fp:
            wall_start = time.monotonic()
            base_t_us: int | None = None

            for datagram in _records_from(fp, start_offset):
                if self._stop:
                    return
                if not self.playing:
                    await self._await_resume()
                    if self._stop:
                        return
                if self._seek_to is not None:
                    return  # unwind so run() can restart the file at the new offset

                t_us = _peek_timestamp(datagram)
                if t_us is None:
                    continue

                if self.first_t_us is None:
                    self.first_t_us = t_us
                if target_seek is not None and t_us < target_seek:
                    # The index is coarse (one entry per 256 frames); scan forward from the
                    # indexed frame to the one actually asked for.
                    continue

                if base_t_us is None:
                    base_t_us = t_us
                    wall_start = time.monotonic()

                if self.speed > 0:
                    due = wall_start + (t_us - base_t_us) / 1e6 / self.speed
                    delay = due - time.monotonic()
                    if delay > _SLEEP_FLOOR_S:
                        await self._sleep_until_due(delay)
                        if self._stop or self._seek_to is not None:
                            return
                elif self.frames_sent % 512 == 0:
                    # Full-speed replay must still yield, or the event loop starves and the
                    # clients watching the replay never receive any of it.
                    await asyncio.sleep(0)

                self.position_us = t_us
                self.last_t_us = t_us
                self.frames_sent += 1
                result = self.sink(datagram, time.time())
                if result is not None:
                    await result

    async def _sleep_until_due(self, delay: float) -> None:
        """Wait for a frame to fall due, or for stop/seek — whichever comes first.

        A plain sleep here is a promise the recording is not obliged to keep. Nothing says the
        frames are evenly spaced: a node that was off for a minute leaves a minute-long hole,
        and replaying it means sleeping through the whole thing. A `stop()` arriving in that
        window would not be noticed until the far side of the gap, long past the two seconds the
        hub is willing to wait before it gives up — so the stop path has to be able to reach into
        the wait rather than only being seen between frames.

        Clearing `_gate` here cannot swallow a resume, because this is only reached with the
        replay already playing. Same clear-then-check-then-wait order as `_await_resume`, for the
        same reason: a stop or seek landing between the check and the wait must not be lost.
        """
        self._gate.clear()
        if self._stop or self._seek_to is not None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._gate.wait(), delay)

    async def _await_resume(self) -> None:
        """Park while paused. Returns on resume, on stop, or on a seek.

        The pause state used to *be* the event: `pause` cleared it and `resume` set it. That made
        it the only way to wake a parked pump, so `seek` had to set it too — which unblocked the
        wait and left the latch open. `run()` would then restart the file at the new offset and
        stream at full rate with `playing` still False, so the browser showed "paused" over a
        replay that was running. Scrubbing the timeline of a paused replay is precisely when you
        least want it to start playing.

        So `playing` is the state and `_gate` is only a doorbell. Clear-then-check-then-wait, so
        a resume landing between the check and the wait cannot be lost.
        """
        while True:
            self._gate.clear()
            if self.playing or self._stop or self._seek_to is not None:
                return
            await self._gate.wait()

    def _offset_for(self, t_us: int) -> int:
        """Byte offset of the last indexed frame at or before `t_us`. 0 if there is no index."""
        if not self._stamps:
            return 0
        i = bisect.bisect_right(self._stamps, t_us) - 1
        return self._index[i][1] if i >= 0 else 0


def _records_from(fp, offset: int) -> Iterator[bytes]:
    """Yield length-prefixed datagrams starting at a byte offset (0 means after the header)."""
    if offset <= 0:
        magic = fp.read(len(REC_MAGIC))
        if magic != REC_MAGIC:
            raise ProtocolError(f"not a CSI recording (magic {magic!r})")
    else:
        fp.seek(offset)

    while True:
        raw = fp.read(_LEN.size)
        if len(raw) < _LEN.size:
            return
        (length,) = _LEN.unpack(raw)
        payload = fp.read(length)
        if len(payload) < length:
            return
        yield payload


def _peek_timestamp(datagram: bytes) -> int | None:
    """Read the device timestamp without building a Frame — replay does this per record.

    Bounded by the *v1* header size on purpose: the timestamp sits at the same offset in every
    version, and a recording made before v2 existed is exactly the thing replay has to keep
    working on.
    """
    if len(datagram) < HEADER_SIZE_V1:
        return None
    return int.from_bytes(datagram[_TIMESTAMP_AT : _TIMESTAMP_AT + 8], "little")
