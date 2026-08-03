"""Desired configuration for nodes that accept control, and what they report back.

The uplink is one-way UDP, so control rides on HTTP instead: the browser PATCHes what it wants
here, the node polls GET and applies it, then POSTs what it actually did. The server never
pushes — a node behind any NAT or firewall that lets it send UDP out can also poll HTTP out,
and a node that never polls (an ESP32, a replayed recording) simply shows no applied state,
which the UI reads as "this node does not take control".

Desired state and the scan request counter are persisted, because they are operator decisions:
a Pi that reboots should come back on the channel it was told to monitor, not on whatever its
association happens to be. Applied state and scan results are runtime-only — the node re-reports
within one poll interval, and stale hardware facts are worse than absent ones.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("csi.nodecontrol")

STIMULUS_MODES = ("auto", "always", "off")

# Channels that exist somewhere in the world at the widths nexmon_csi can be asked for.
# Validation is deliberately loose about regulatory domains — the node is the one with the
# radio, and it will report what actually happened — but strict about shape, because a typo'd
# spec would otherwise be stored forever and re-applied on every service start.
_CHANNEL_RE = re.compile(r"^(\d{1,3})/(20|40|80)$")
_MAX_CHANNEL = 177


def _valid_channel(spec: str) -> bool:
    if spec == "auto":
        return True
    match = _CHANNEL_RE.match(spec)
    if match is None:
        return False
    return 1 <= int(match.group(1)) <= _MAX_CHANNEL


class NodeControlStore:
    """Per-node desired config, revisioned, with the node's own report alongside.

    Thread-safe the same way the hub is used: FastAPI handlers run on the event loop thread,
    but persistence happens inline and the ingest path never touches this — a plain lock is
    plenty at the rate configuration changes.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # node_id -> {"desired": {...}, "revision": int, "scan_rev": int}
        self._desired: dict[int, dict[str, Any]] = {}
        # node_id -> {"applied": {...}, "reported_rev": int, "scan": {...} | None, "ts": float}
        self._reported: dict[int, dict[str, Any]] = {}
        self._load()

    # -- persistence ----------------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("ignoring unreadable %s: %s", self._path, exc)
            return
        nodes = raw.get("nodes", {})
        if not isinstance(nodes, dict):
            return
        for key, entry in nodes.items():
            try:
                node_id = int(key)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            desired = entry.get("desired", {})
            self._desired[node_id] = {
                "desired": {
                    "channel": desired.get("channel", "auto"),
                    "stimulus": desired.get("stimulus", "auto"),
                },
                "revision": int(entry.get("revision", 0)),
                "scan_rev": int(entry.get("scan_rev", 0)),
            }

    def _save(self) -> None:
        payload = {"nodes": {str(node_id): entry for node_id, entry in self._desired.items()}}
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("could not persist node control to %s: %s", self._path, exc)

    # -- the browser's side ----------------------------------------------------------------

    def get(self, node_id: int) -> dict:
        with self._lock:
            return self._entry(node_id)

    def patch(self, node_id: int, patch: dict) -> dict:
        """Apply a partial desired-config update. Unknown keys and bad values are dropped,
        for the same reason `Hub.update_config` drops them: a typo from a client must never
        become a stored instruction."""
        with self._lock:
            entry = self._desired.setdefault(node_id, _fresh_entry())
            desired = entry["desired"]
            changed = False

            channel = patch.get("channel")
            if (
                isinstance(channel, str)
                and _valid_channel(channel)
                and channel != desired["channel"]
            ):
                desired["channel"] = channel
                changed = True

            stimulus = patch.get("stimulus")
            if stimulus in STIMULUS_MODES and stimulus != desired["stimulus"]:
                desired["stimulus"] = stimulus
                changed = True

            if changed:
                entry["revision"] += 1
                self._save()
            return self._entry(node_id)

    def request_scan(self, node_id: int) -> dict:
        with self._lock:
            entry = self._desired.setdefault(node_id, _fresh_entry())
            entry["scan_rev"] += 1
            self._save()
            return self._entry(node_id)

    # -- the node's side -------------------------------------------------------------------

    def report(self, node_id: int, body: dict) -> dict:
        """Store what a node says it has applied. The report's `revision` is the desired
        revision the node had seen when it applied; the UI compares it against the current
        one to render "pending" honestly."""
        applied = body.get("applied")
        scan = body.get("scan")
        with self._lock:
            reported = self._reported.setdefault(node_id, {"scan": None})
            reported["applied"] = applied if isinstance(applied, dict) else None
            reported["reported_rev"] = int(body.get("revision", 0))
            reported["ts"] = time.time()
            if isinstance(scan, dict) and isinstance(scan.get("aps"), list):
                reported["scan"] = {"ts": time.time(), "aps": scan["aps"]}
                reported["scan_rev"] = int(body.get("scan_rev", 0))
            return self._entry(node_id)

    # -- shared ----------------------------------------------------------------------------

    def node_ids(self) -> set[int]:
        with self._lock:
            return set(self._desired) | set(self._reported)

    def _entry(self, node_id: int) -> dict:
        """One node's full control picture. Caller holds the lock."""
        desired = self._desired.get(node_id, _fresh_entry())
        reported = self._reported.get(node_id, {})
        return {
            "node_id": node_id,
            "desired": dict(desired["desired"]),
            "revision": desired["revision"],
            "scan_rev": desired["scan_rev"],
            "applied": reported.get("applied"),
            "reported_rev": reported.get("reported_rev"),
            "reported_ts": reported.get("ts"),
            "scan": reported.get("scan"),
            "reported_scan_rev": reported.get("scan_rev"),
        }


def _fresh_entry() -> dict[str, Any]:
    return {"desired": {"channel": "auto", "stimulus": "auto"}, "revision": 0, "scan_rev": 0}
