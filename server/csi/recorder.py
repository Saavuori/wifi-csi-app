"""Append every frame to disk, byte for byte.

This is Phase 2, and it comes before any analysis on purpose: without it, every iteration on
the breathing detector costs a trip to go and lie down in the room. It is the single decision
in the project that buys back the most time.

What is stored is the datagram exactly as it arrived, length-prefixed. Not a parsed
representation, not JSON. That makes "a recording replays identically through the live
pipeline" a property of the format rather than a thing to keep testing for — the replayer hands
the same bytes to the same parser.
"""

from __future__ import annotations

import time
from pathlib import Path

from .protocol import (
    INDEX_ENTRY,
    INDEX_STRIDE,
    REC_MAGIC,
    Frame,
    ProtocolError,
    iter_records,
    record_bytes,
)
from .sessions import Session, SessionStore


class Recorder:
    """Writes one session. Not thread-safe; the hub owns it on the ingest task."""

    def __init__(self, store: SessionStore, session: Session) -> None:
        self.store = store
        self.session = session
        self.path = store.file_for(session)
        self._fp = open(self.path, "ab")
        self._idx = open(self.path.with_suffix(self.path.suffix + ".idx"), "ab")
        if self._fp.tell() == 0:
            self._fp.write(REC_MAGIC)
        self._offset = self._fp.tell()
        self._since_index = 0
        self._nodes: set[int] = set(session.node_ids)
        self._last_flush = time.monotonic()

    @property
    def closed(self) -> bool:
        return self._fp.closed

    def write(self, datagram: bytes, frame: Frame) -> None:
        blob = record_bytes(datagram)

        if self._since_index == 0:
            self._idx.write(INDEX_ENTRY.pack(frame.timestamp, self._offset))
        self._since_index = (self._since_index + 1) % INDEX_STRIDE

        self._fp.write(blob)
        self._offset += len(blob)

        session = self.session
        session.frames += 1
        session.bytes = self._offset
        session.last_t_us = frame.timestamp
        if session.first_t_us is None:
            session.first_t_us = frame.timestamp
        if frame.node_id not in self._nodes:
            self._nodes.add(frame.node_id)
            session.node_ids = sorted(self._nodes)
        if not session.n_sub:
            session.n_sub = frame.n_sub

        # Flush on a timer rather than per frame. An 8-hour overnight run is ~3 million frames;
        # fsyncing each one would dominate the server's work for no benefit, and the OS buffer
        # is only at risk in a hard power loss, which truncates the recording harmlessly.
        now = time.monotonic()
        if now - self._last_flush > 5.0:
            self.flush()
            self._last_flush = now

    def flush(self) -> None:
        if not self._fp.closed:
            self._fp.flush()
            self._idx.flush()
        self.store.update(self.session)

    def close(self) -> None:
        if self._fp.closed:
            return
        self._fp.flush()
        self._fp.close()
        self._idx.flush()
        self._idx.close()
        self.session.ended_at = time.time()
        self.store.update(self.session)


def read_index(path: Path) -> list[tuple[int, int]]:
    """Load the sidecar index as (timestamp_us, byte_offset) pairs.

    Returns an empty list if the index is missing or unreadable. It is an optimization, never a
    source of truth: a crash mid-write leaves a short index, which is fine, because seeking
    falls back to scanning from the last entry that does exist.
    """
    idx_path = path.with_suffix(path.suffix + ".idx")
    try:
        raw = idx_path.read_bytes()
    except OSError:
        return []

    n = len(raw) // INDEX_ENTRY.size
    return [INDEX_ENTRY.unpack_from(raw, i * INDEX_ENTRY.size) for i in range(n)]


def scan_recording(path: Path) -> dict:
    """Walk a recording and summarize it. Used to repair session metadata after a crash."""
    from .protocol import parse_frame

    frames = 0
    nodes: set[int] = set()
    first: int | None = None
    last: int | None = None
    n_sub = 0
    bad = 0

    with open(path, "rb") as fp:
        for _, datagram in iter_records(fp):
            try:
                frame = parse_frame(datagram)
            except ProtocolError:
                bad += 1
                continue
            frames += 1
            nodes.add(frame.node_id)
            n_sub = n_sub or frame.n_sub
            if first is None:
                first = frame.timestamp
            last = frame.timestamp

    return {
        "frames": frames,
        "node_ids": sorted(nodes),
        "first_t_us": first,
        "last_t_us": last,
        "n_sub": n_sub,
        "bad": bad,
        "bytes": path.stat().st_size,
    }
