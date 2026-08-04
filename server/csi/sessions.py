"""Session metadata: what was recorded, when, and what was going on in the room.

Deliberately a single JSON file rather than a database. The whole point of the recorder is that
you can iterate on the detector without going back to lie on the floor, and that works best if
the metadata is something you can also open in an editor and fix by hand when you mislabel a
session at 1am.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

SESSIONS_FILE = "sessions.json"

# The labels from the plan's capture table. Free-form labels are allowed; these exist so the UI
# can offer them and so the analysis tooling can find a baseline without being told.
SUGGESTED_LABELS = (
    "empty-room",
    "walking",
    "seated-still",
    "seated-still-positions",
    "overnight",
)

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG.sub("-", text.strip().lower()).strip("-") or "session"


@dataclass(slots=True)
class Session:
    id: str
    label: str
    path: str  # relative to the recordings directory
    started_at: float  # wall clock, seconds
    ended_at: float | None = None
    frames: int = 0
    bytes: int = 0
    node_ids: list[int] = field(default_factory=list)
    first_t_us: int | None = None
    last_t_us: int | None = None
    n_sub: int = 0
    notes: str = ""

    @property
    def duration_s(self) -> float:
        """Duration by device clock where possible — it is the one that is actually uniform."""
        if self.first_t_us is not None and self.last_t_us is not None:
            return (self.last_t_us - self.first_t_us) / 1e6
        if self.ended_at:
            return self.ended_at - self.started_at
        return max(0.0, time.time() - self.started_at)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["duration_s"] = round(self.duration_s, 2)
        d["active"] = self.ended_at is None
        return d


class SessionStore:
    """Loads and saves the session list. Cheap enough to re-read; kept in memory anyway."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / SESSIONS_FILE
        self._sessions: dict[str, Session] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._sessions = {}
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt index must not take the server down — the recordings themselves are
            # still on disk and are the actual data. Move it aside and start a fresh one.
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
            try:
                self.path.rename(backup)
            except OSError:
                pass
            self._sessions = {}
            return

        known = {f.name for f in Session.__dataclass_fields__.values()}
        self._sessions = {}
        for item in raw.get("sessions", []):
            try:
                self._sessions[item["id"]] = Session(
                    **{k: v for k, v in item.items() if k in known}
                )
            except (TypeError, KeyError):
                continue

    def save(self) -> None:
        """Atomic replace, so a crash mid-write cannot lose the whole index."""
        payload = {
            "version": 1,
            "sessions": [s.as_dict() for s in self.sorted()],
        }
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fp:
                json.dump(payload, fp, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def sorted(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.started_at, reverse=True)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def create(self, label: str, notes: str = "") -> Session:
        started = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
        base = f"{stamp}-{slugify(label)}"
        session_id = base
        n = 1
        while session_id in self._sessions:
            n += 1
            session_id = f"{base}-{n}"

        session = Session(
            id=session_id,
            label=label,
            path=f"{session_id}.csi",
            started_at=started,
            notes=notes,
        )
        self._sessions[session_id] = session
        self.save()
        return session

    def update(self, session: Session) -> None:
        self._sessions[session.id] = session
        self.save()

    def delete(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        for suffix in ("", ".idx"):
            (self.directory / (session.path + suffix)).unlink(missing_ok=True)
        self.save()
        return True

    def file_for(self, session: Session) -> Path:
        return self.directory / session.path

    def total_bytes(self) -> int:
        """Bytes on disk across every recording, from the files rather than the metadata.

        The metadata's `bytes` is flushed periodically and is behind by design; a retention
        decision has to be made against what the disk actually holds.
        """
        total = 0
        for path in self.directory.glob("*.csi"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def prune(self, budget_bytes: int, *, keep: Session | None = None) -> list[str]:
        """Delete oldest-first until the recordings fit in `budget_bytes`. Returns what went.

        A node left running writes about a gigabyte a day. On the SD card that is the default
        home for it, that is a wear problem as much as a capacity one, and the failure mode is
        the worst kind: the appliance works for a month and then stops, having filled the card
        the operating system is also on.

        Oldest-first, and never the session currently being written — deleting the file under
        the recorder would lose the session anyone is actually watching. A labelled session is
        no more protected than any other: this is a disk limit, not a judgement about value,
        and pretending otherwise would just mean the limit silently stops working for someone
        who labels everything.
        """
        if budget_bytes <= 0:
            return []
        total = self.total_bytes()
        if total <= budget_bytes:
            return []

        removed: list[str] = []
        # Oldest first; `sorted()` is newest-first.
        for session in reversed(self.sorted()):
            if total <= budget_bytes:
                break
            if keep is not None and session.id == keep.id:
                continue
            size = 0
            for suffix in ("", ".idx"):
                path = self.directory / (session.path + suffix)
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
            if self.delete(session.id):
                removed.append(session.id)
                total -= size
        return removed
