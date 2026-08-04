"""Zones, the examples recorded for them, and the models built from those.

Two kinds of thing live here and they have opposite storage needs, which is why they are stored
differently. The zone definitions are a handful of names the user typed and belong in a JSON file
they can open and fix, exactly as `sessions.py` argues. The examples are numeric arrays that
nobody will ever hand-edit and go in one `.npz` each.

They are kept well away from `recordings/`. A recording is rolling capture that the retention
sweep is right to delete when the disk fills; an example someone walked into the kitchen to
record is training data, and deleting it silently would break the feature it was recorded for.
So: a separate directory, a separate budget, and a refusal rather than a prune when that budget
is reached.

What is stored per example is the *raw* window, not a resampled or feature-reduced one. Storing
features alone would mean every improvement to `zone_features` invalidated every example in the
house; storing a resampled window would bake in the interpolation and hide the holes that
`Coverage` exists to catch. The features are cached alongside, keyed by the feature version and
by the gate they were computed under, and silently recomputed when either moves.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .dsp.zones import (
    FEATURE_VERSION,
    Binding,
    SampleVectors,
    ZoneConfig,
    ZoneModel,
    build_gate,
    build_model,
    cross_validate,
    mask_key,
    sub_windows,
    zone_features,
)
from .ring import Window

log = logging.getLogger("csi.zones")

ZONES_FILE = "zones.json"
SAMPLES_DIR = "samples"

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG.sub("-", text.strip().lower()).strip("-") or "zone"


class ZoneStoreFull(Exception):
    """The examples directory is at its budget. Raised rather than pruning — see the module
    docstring."""


@dataclass(slots=True)
class Zone:
    id: str
    name: str
    created_at: float
    notes: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ZoneSample:
    """One recorded example. Everything here except `quiet` is a fact about the capture."""

    id: str
    zone_id: str
    node_id: int
    recorded_at: float
    duration_s: float
    frames: int
    rate_hz: float
    n_sub: int
    mask_key: str
    src_mac: str
    channel: int
    # Median motion index over the capture and the presence detector's enter threshold at the
    # time, kept as two numbers rather than one verdict. An example recorded while nothing moved
    # is a fingerprint of an empty room wearing a zone's name — but "nothing moved" is not
    # something this can assert on its own, because the threshold is only meaningful if presence
    # calibrated in an empty room. Reporting both lets the UI state the comparison it actually
    # made instead of a conclusion it cannot support.
    motion_median: float = 0.0
    motion_enter: float = 0.0
    bytes: int = 0

    @property
    def quiet(self) -> bool:
        """Derived, never stored, so the flag and the numbers behind it cannot disagree.

        Stored separately they did: examples written before `motion_enter` existed kept their
        `quiet` and lost the threshold, and the UI rendered "motion ran at 8.8, below the 0.0".
        A zero threshold means nobody measured one, which is a reason to say nothing rather than
        a reason to warn.
        """
        return self.motion_enter > 0.0 and self.motion_median < self.motion_enter

    def binding(self) -> Binding:
        return Binding(
            node_id=self.node_id,
            n_sub=self.n_sub,
            mask_key=self.mask_key,
            src_mac=self.src_mac,
        )

    def as_dict(self) -> dict:
        return {**asdict(self), "quiet": self.quiet}


@dataclass(slots=True)
class _NodeModels:
    """Every model for one node, keyed by the link its examples were recorded on."""

    by_binding: dict[Binding, ZoneModel] = field(default_factory=dict)
    latest: Binding | None = None


class ZoneStore:
    """Definitions, examples and models. Rebuilds are CPU-bound; call `rebuild` in a thread."""

    def __init__(
        self,
        directory: Path,
        config: ZoneConfig | None = None,
        max_bytes: int = 200 * 1024**2,
    ) -> None:
        self.directory = Path(directory)
        self.samples_dir = self.directory / SAMPLES_DIR
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / ZONES_FILE
        self.config = config or ZoneConfig()
        self.max_bytes = max_bytes

        self._lock = threading.RLock()
        self._zones: dict[str, Zone] = {}
        self._samples: dict[str, ZoneSample] = {}
        self._models: dict[int, _NodeModels] = {}
        self._report: dict = {"samples": [], "zones": {}, "confusion": {}, "indistinguishable": []}
        self._load()

    # -- persistence ------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            # Same reasoning as SessionStore: the arrays on disk are the actual data and are
            # still there. Move the index aside rather than refusing to start.
            log.warning("ignoring unreadable %s: %s", self.path, exc)
            try:
                self.path.rename(self.path.with_suffix(f".corrupt-{int(time.time())}.json"))
            except OSError:
                pass
            return

        for item in raw.get("zones", []):
            try:
                self._zones[item["id"]] = Zone(
                    id=str(item["id"]),
                    name=str(item.get("name", item["id"])),
                    created_at=float(item.get("created_at", 0.0)),
                    notes=str(item.get("notes", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue

        # `quiet` is deliberately not among these: it is a property now, and older files still
        # carry it as a stored key. Filtering to declared fields drops it without complaint.
        known = {f.name for f in ZoneSample.__dataclass_fields__.values()}
        for item in raw.get("samples", []):
            try:
                sample = ZoneSample(**{k: v for k, v in item.items() if k in known})
            except (TypeError, KeyError):
                continue
            if sample.zone_id not in self._zones:
                continue
            if not self._sample_path(sample.id).exists():
                # The array is gone — a half-finished delete, or someone tidying by hand. The
                # metadata alone cannot train anything, so it is not worth keeping.
                log.warning("zone sample %s has no data file; dropping it", sample.id)
                continue
            self._samples[sample.id] = sample

    def _save(self) -> None:
        payload = {
            "version": 1,
            "zones": [z.as_dict() for z in self.sorted_zones()],
            "samples": [s.as_dict() for s in self.sorted_samples()],
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

    def _sample_path(self, sample_id: str) -> Path:
        return self.samples_dir / f"{sample_id}.npz"

    # -- zones ------------------------------------------------------------------------------

    def sorted_zones(self) -> list[Zone]:
        return sorted(self._zones.values(), key=lambda z: z.created_at)

    def sorted_samples(self) -> list[ZoneSample]:
        return sorted(self._samples.values(), key=lambda s: s.recorded_at)

    def get_zone(self, zone_id: str) -> Zone | None:
        return self._zones.get(zone_id)

    def create_zone(self, name: str, notes: str = "") -> Zone:
        with self._lock:
            base = slugify(name)
            zone_id = base
            n = 1
            while zone_id in self._zones:
                n += 1
                zone_id = f"{base}-{n}"
            zone = Zone(
                id=zone_id,
                name=name.strip() or zone_id,
                created_at=time.time(),
                notes=notes,
            )
            self._zones[zone_id] = zone
            self._save()
            return zone

    def update_zone(self, zone_id: str, *, name: str | None = None, notes: str | None = None):
        with self._lock:
            zone = self._zones.get(zone_id)
            if zone is None:
                return None
            if name is not None and name.strip():
                zone.name = name.strip()
            if notes is not None:
                zone.notes = notes
            self._save()
            return zone

    def delete_zone(self, zone_id: str) -> bool:
        with self._lock:
            if self._zones.pop(zone_id, None) is None:
                return False
            for sample in [s for s in self._samples.values() if s.zone_id == zone_id]:
                self._remove_sample_files(sample.id)
                del self._samples[sample.id]
            self._save()
        self.rebuild()
        return True

    # -- samples ----------------------------------------------------------------------------

    def total_bytes(self) -> int:
        total = 0
        for path in self.samples_dir.glob("*.npz"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def add_sample(
        self,
        zone_id: str,
        window: Window,
        *,
        node_id: int,
        src_mac: str,
        channel: int,
        motion_median: float,
        motion_enter: float,
    ) -> ZoneSample:
        """Persist one captured example and rebuild. Runs off the event loop."""
        with self._lock:
            if zone_id not in self._zones:
                raise KeyError(zone_id)
            if self.total_bytes() >= self.max_bytes:
                raise ZoneStoreFull(
                    f"the zone examples directory is at its {self.max_bytes / 1024**2:.0f} MB "
                    "budget; delete some examples before recording more"
                )

            base = f"{zone_id}-{time.strftime('%Y%m%d-%H%M%S', time.localtime())}"
            sample_id = base
            n = 1
            while sample_id in self._samples:
                n += 1
                sample_id = f"{base}-{n}"

            path = self._sample_path(sample_id)
            np.savez_compressed(
                path,
                t_us=window.t_us.astype(np.int64),
                amp=window.amp.astype(np.float32),
                agc=window.agc.astype(bool),
                rssi=window.rssi.astype(np.int8),
                mask=window.mask.astype(bool),
            )

            sample = ZoneSample(
                id=sample_id,
                zone_id=zone_id,
                node_id=node_id,
                recorded_at=time.time(),
                duration_s=round(window.duration_s, 2),
                frames=len(window),
                rate_hz=round(window.rate_hz, 2),
                n_sub=window.n_sub,
                mask_key=mask_key(window.mask),
                src_mac=src_mac,
                channel=channel,
                motion_median=round(float(motion_median), 3),
                motion_enter=round(float(motion_enter), 3),
                bytes=path.stat().st_size,
            )
            self._samples[sample_id] = sample
            self._save()

        self.rebuild()
        return sample

    def delete_sample(self, sample_id: str) -> bool:
        with self._lock:
            if self._samples.pop(sample_id, None) is None:
                return False
            self._remove_sample_files(sample_id)
            self._save()
        self.rebuild()
        return True

    def _remove_sample_files(self, sample_id: str) -> None:
        path = self._sample_path(sample_id)
        path.unlink(missing_ok=True)
        # The feature cache too. Left behind it is an orphan that counts against the budget and
        # that nothing will ever read, because its key is a sample id that no longer exists.
        path.with_suffix(".features.npz").unlink(missing_ok=True)

    def _load_window(self, sample_id: str) -> Window | None:
        try:
            with np.load(self._sample_path(sample_id)) as data:
                return Window(
                    t_us=data["t_us"],
                    amp=data["amp"],
                    agc=data["agc"],
                    rssi=data["rssi"],
                    mask=data["mask"],
                )
        except (OSError, KeyError, ValueError) as exc:
            log.warning("zone sample %s is unreadable: %s", sample_id, exc)
            return None

    # -- caching --------------------------------------------------------------------------

    def _cached_vectors(self, sample_id: str, gate_key: str) -> np.ndarray | None:
        """Features from a previous rebuild, if they were computed the same way.

        Keyed by the gate as well as the feature version, because the gate is a quantile over
        every example in the set — adding one can move it, and comparing a vector built under
        one gate with a centroid built under another is exactly the misalignment this whole
        module is arranged to prevent.
        """
        path = self._sample_path(sample_id).with_suffix(".features.npz")
        try:
            with np.load(path) as data:
                if int(data["feature_version"]) != FEATURE_VERSION:
                    return None
                if str(data["gate_key"]) != gate_key:
                    return None
                return np.asarray(data["vectors"], dtype=np.float64)
        except (OSError, KeyError, ValueError):
            return None

    def _cache_vectors(self, sample_id: str, gate_key: str, vectors: np.ndarray) -> None:
        path = self._sample_path(sample_id).with_suffix(".features.npz")
        try:
            np.savez_compressed(
                path,
                vectors=vectors,
                gate_key=np.array(gate_key),
                feature_version=np.array(FEATURE_VERSION),
            )
        except OSError as exc:
            log.warning("could not cache zone features for %s: %s", sample_id, exc)

    # -- models ---------------------------------------------------------------------------

    def rebuild(self) -> None:
        """Recompute gates, feature vectors, centroids and the cross-validation report.

        Grouped by (node, link) rather than by node alone. A fingerprint recorded against one
        access point says nothing about the same room seen through another, so rather than
        arbitrating between them — and throwing away examples that are perfectly good for a link
        the mesh may well put the node back on — each link gets its own model and the classifier
        asks for the one matching the frames actually arriving.
        """
        with self._lock:
            samples = self.sorted_samples()
            groups: dict[tuple[int, Binding], list[ZoneSample]] = {}
            for sample in samples:
                groups.setdefault((sample.node_id, sample.binding()), []).append(sample)

            models: dict[int, _NodeModels] = {}
            all_vectors: list[SampleVectors] = []

            for (node_id, binding), members in groups.items():
                model, vectors = self._build_group(binding, members)
                node_models = models.setdefault(node_id, _NodeModels())
                node_models.by_binding[binding] = model
                all_vectors.extend(vectors)

            for node_id, node_models in models.items():
                latest = max(
                    (s for s in samples if s.node_id == node_id),
                    key=lambda s: s.recorded_at,
                    default=None,
                )
                node_models.latest = latest.binding() if latest is not None else None

            self._models = models
            # Cross-validation runs per group as well: holding out an example only means
            # anything against the examples it would actually be compared with.
            self._report = self._validate(groups, all_vectors)

    def _build_group(
        self, binding: Binding, members: list[ZoneSample]
    ) -> tuple[ZoneModel, list[SampleVectors]]:
        windows: dict[str, Window] = {}
        profiles: list[np.ndarray] = []
        mask: np.ndarray | None = None

        for sample in members:
            window = self._load_window(sample.id)
            if window is None:
                continue
            windows[sample.id] = window
            if mask is None:
                mask = window.mask
            valid = np.flatnonzero(window.mask)
            profile = np.full(window.n_sub, np.nan, dtype=np.float64)
            if valid.size:
                block = window.amp[:, valid]
                usable = np.isfinite(block).all(axis=0)
                if usable.any():
                    profile[valid[usable]] = block[:, usable].mean(axis=0)
            profiles.append(profile)

        if mask is None or not profiles:
            return ZoneModel(binding=binding, config=self.config), []

        gate = build_gate(np.vstack(profiles), mask, self.config)
        key = mask_key(np.isin(np.arange(mask.size), gate))

        vectors: list[SampleVectors] = []
        for sample in members:
            window = windows.get(sample.id)
            if window is None:
                continue
            cached = self._cached_vectors(sample.id, key)
            if cached is None:
                extracted = [
                    v
                    for v in (
                        zone_features(sub, gate, self.config)
                        for sub in sub_windows(window, self.config)
                    )
                    if v is not None
                ]
                cached = np.vstack(extracted) if extracted else np.zeros((0, 0))
                self._cache_vectors(sample.id, key, cached)
            vectors.append(SampleVectors(sample.id, sample.zone_id, cached))

        model = build_model(vectors, binding=binding, gate=gate, config=self.config)
        return model, vectors

    def _validate(
        self, groups: dict[tuple[int, Binding], list[ZoneSample]], vectors: list[SampleVectors]
    ) -> dict:
        by_id = {v.sample_id: v for v in vectors}
        merged: dict = {"samples": [], "zones": {}, "confusion": {}, "indistinguishable": []}
        for members in groups.values():
            group_vectors = [by_id[s.id] for s in members if s.id in by_id]
            if not group_vectors:
                continue
            report = cross_validate(group_vectors, self.config)
            merged["samples"].extend(report["samples"])
            # One node/link at a time, so a zone taught on two links keeps the better picture
            # rather than the last one written.
            for zone_id, stats in report["zones"].items():
                current = merged["zones"].get(zone_id)
                if current is None or (stats["scored"] or 0) > (current["scored"] or 0):
                    merged["zones"][zone_id] = stats
            for zone_id, counts in report["confusion"].items():
                target = merged["confusion"].setdefault(zone_id, {})
                for predicted, count in counts.items():
                    target[predicted] = target.get(predicted, 0) + count
            merged["indistinguishable"].extend(report["indistinguishable"])
        return merged

    def model_for(self, node_id: int, binding: Binding | None) -> ZoneModel:
        """The model to classify this node's frames with.

        An exact match when the node is on a link it has examples for. Otherwise the model for
        the link it most recently had examples on — which will not match, and is returned
        precisely so the classifier can say *why* rather than reporting a blank.
        """
        with self._lock:
            node_models = self._models.get(node_id)
            if node_models is None:
                return ZoneModel(config=self.config)
            if binding is not None:
                exact = node_models.by_binding.get(binding)
                if exact is not None:
                    return exact
            if node_models.latest is not None:
                fallback = node_models.by_binding.get(node_models.latest)
                if fallback is not None:
                    return fallback
            return ZoneModel(config=self.config)

    # -- introspection --------------------------------------------------------------------

    def report(self) -> dict:
        """Everything the zones view renders, in one payload."""
        with self._lock:
            verdicts = {item["sample_id"]: item for item in self._report.get("samples", [])}
            samples = []
            for sample in self.sorted_samples():
                entry = sample.as_dict()
                entry["verdict"] = verdicts.get(sample.id, {}).get("verdict", "unscored")
                entry["predicted"] = verdicts.get(sample.id, {}).get("predicted")
                samples.append(entry)

            return {
                "zones": [z.as_dict() for z in self.sorted_zones()],
                "samples": samples,
                "accuracy": self._report.get("zones", {}),
                "confusion": self._report.get("confusion", {}),
                "indistinguishable": self._report.get("indistinguishable", []),
                "bytes": self.total_bytes(),
                "max_bytes": self.max_bytes,
                # Which extractor built the cached vectors. Not used by the UI, but it is the
                # first thing worth checking when zones stop matching after an upgrade.
                "feature_version": FEATURE_VERSION,
            }
