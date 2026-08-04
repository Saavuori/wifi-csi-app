"""Phase 7: which part of the room the movement is in.

This is fingerprint classification, not localization, and the difference is the whole design.
Nothing here computes a position. The user records examples of movement in each place they care
about, and this matches live movement against those recordings by nearest centroid. It can only
separate places the recordings actually separate — two zones sitting on the same Fresnel ellipse
relative to the node/access-point line produce near-identical fingerprints, and no quantity of
extra examples will fix that. `cross_validate` exists to say so out loud rather than letting the
classifier guess between them forever.

Three decisions carry most of the weight:

**The feature is a shape, not a level.** Per-subcarrier motion-band power, expressed in dB about
its own mean and then L2-normalized. Absolute power tracks how vigorously the person moved, which
is a property of the person and the day; the *relative* profile across frequency is the Fresnel
selectivity that tracks where they were. Band power rather than raw variance for the same reason
`rank_for_band` uses it — it rejects slow drift and thermal noise without a second mechanism. The
dB and the centring are not cosmetic; see `_band_profile` for what happens without them.

**The magnitude gate is fixed by the model, not by the window.** `magnitude_gate` takes a
quantile of whatever window it is handed, so a sample and a query would gate differently and the
two vectors would no longer be indexed by the same subcarriers. The gate here is computed once
from the stored examples and applied identically to both sides.

**Nearest centroid, cosine distance, and a calibrated "none of these".** With a handful of
20-second examples per zone there is nothing to train anything larger on, deleting an example
must be free (it is a centroid recomputation), and the distances have to be legible in the UI so
a bad answer is diagnosable. The rejection threshold comes from the spread of a zone's own
examples about its centroid, so it means the same thing in a room with two well-separated zones
and a room with five overlapping ones.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

import numpy as np

from ..ring import History, Window
from .util import band_power, detrend, robust_std, uniform_resample, welch_psd

# Bumped whenever anything in `zone_features` changes the meaning of a vector. The store keeps
# the raw window for every example, so a bump recomputes from that rather than asking the user to
# walk round the house re-recording every zone.
FEATURE_VERSION = 1


@dataclass(slots=True)
class ZoneConfig:
    # Movement, not respiration. The lower edge sits above the breathing band so a motionless
    # person does not fingerprint as one who is moving; the upper edge is where amplitude
    # modulation from a walking body has died away and only noise is left.
    band: tuple[float, float] = (0.25, 5.0)
    fs: float = 20.0

    # One feature vector per `window_s`, taken every `hop_s`. A 20 s example therefore yields
    # four overlapping vectors rather than one, which is where the within-zone spread the
    # rejection threshold is calibrated from comes from — and it costs nothing.
    window_s: float = 8.0
    hop_s: float = 4.0
    sample_s: float = 20.0

    gate_quantile: float = 0.25
    coherence_k: int = 8
    # How much of the vector the coherence block accounts for once both blocks are normalized.
    # The profile is the discriminative part; coherence mostly separates "crossed the direct
    # path" from "disturbed a weak scatterer", which is one coarse axis and should not outvote
    # the other forty.
    coherence_weight: float = 0.35

    # A window with a hole in it is refused for the same reason a breathing window is: the
    # interpolator draws a straight line across the gap and the ramp lands in the band being
    # measured. Here that manufactures motion-band power on every subcarrier at once, which
    # reads as a confident match for whichever zone happens to be closest to flat.
    max_gap_s: float = 0.5
    max_interpolated: float = 0.25

    # Rejection, in units of the model's own geometry rather than absolute distances — the same
    # discipline as the presence detector's thresholds, which are in robust sigmas of the motion
    # index as measured in *that* room rather than in variance units.
    #
    # The absolute scale here is not knowable in advance and is not even stable between rooms:
    # measured on synthetic scenes, two clearly different places sat 0.03 apart in cosine
    # distance while repeats of the same place sat 0.008 apart. Fixed constants picked against
    # one of those numbers make every answer "unknown" in a room with well-separated zones and
    # every answer "ambiguous" in a room with close ones. So both thresholds are fractions of
    # `separation` — the median distance between this model's own centroids — which is exactly
    # the quantity that varies between rooms.
    reject_sigma: float = 3.0
    reject_separation_frac: float = 0.45
    # Last resort only: one zone has no inter-centroid distance to scale against.
    reject_floor: float = 0.005

    # The best two zones must differ by this fraction of the separation, or the answer is
    # "ambiguous". Saying that is more useful than picking the winner of a coin flip.
    margin_frac: float = 0.15
    min_margin: float = 0.002

    # Published label is the majority of the classifications in this window. Raw per-window
    # labels flip at the metrics rate and are unreadable; a couple of seconds of latency is
    # nothing against a person walking between rooms.
    vote_s: float = 3.0


@dataclass(frozen=True, slots=True)
class Binding:
    """The link a set of fingerprints is valid against.

    Not `link_epoch`. That increments on every re-association, including one straight back to the
    access point the node was already on, where nothing about the geometry has changed and the
    recordings are still perfectly good. The BSSID is the thing that says the far antenna moved,
    which is what actually invalidates a fingerprint. (A roam *during* a capture is a different
    problem and is caught separately, by the capture, using the epoch.)

    `mask_key` catches a subcarrier mask change — dropping pilots or the DC-adjacent bins moves
    which column means which subcarrier, and a vector indexed one way cannot be compared with a
    vector indexed the other.
    """

    node_id: int
    n_sub: int
    mask_key: str
    src_mac: str

    @classmethod
    def make(cls, node_id: int, n_sub: int, mask: np.ndarray, src_mac: str) -> Binding:
        return cls(node_id=node_id, n_sub=n_sub, mask_key=mask_key(mask), src_mac=src_mac)

    def as_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "n_sub": self.n_sub,
            "mask_key": self.mask_key,
            "src_mac": self.src_mac,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Binding | None:
        try:
            return cls(
                node_id=int(raw["node_id"]),
                n_sub=int(raw["n_sub"]),
                mask_key=str(raw["mask_key"]),
                src_mac=str(raw.get("src_mac", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def differs_from(self, other: Binding) -> str | None:
        """A human-readable reason these are not the same link, or None if they are."""
        if self.node_id != other.node_id:
            return f"recorded on node {self.node_id}, now looking at node {other.node_id}"
        if self.n_sub != other.n_sub:
            return (
                f"recorded at {self.n_sub} subcarriers, the node is now sending {other.n_sub} "
                "— the bandwidth changed"
            )
        if self.mask_key != other.mask_key:
            return "the subcarrier mask changed since these were recorded"
        if self.src_mac != other.src_mac:
            return (
                f"recorded against access point {self.src_mac or 'unknown'}, now associated "
                f"with {other.src_mac or 'unknown'}"
            )
        return None


def mask_key(mask: np.ndarray) -> str:
    """A short stable identifier for a boolean subcarrier mask.

    Hex of the packed bits. Shorter than the mask, readable in the JSON when something has gone
    wrong, and — unlike a hash — reversible by hand if it ever needs to be.
    """
    return np.packbits(np.asarray(mask, dtype=bool)).tobytes().hex()


# -- features ------------------------------------------------------------------------------


def _nperseg(config: ZoneConfig, n: int) -> int:
    """Segment length for the PSD.

    Same rule as `rank_for_band`: resolve the band rather than merely covering it. The motion
    band is wide, so here the floor of 64 is what binds rather than the band width — and what
    actually matters is that a sample and a query are segmented identically, which they are
    because both come through this function with the same window length.
    """
    want = int(np.ceil(config.fs * 8 / max(config.band[1] - config.band[0], 1e-3)))
    return min(n, max(64, want))


def zone_features(
    window: Window, gate: np.ndarray, config: ZoneConfig
) -> np.ndarray | None:
    """One fingerprint from one window, or None if the window cannot support one.

    `gate` indexes into the full n_sub axis and comes from the model, never from this window —
    see the module docstring.
    """
    if gate.size == 0 or len(window) < 16:
        return None

    block = window.amp[:, gate]
    if not np.isfinite(block).all():
        # A column with a hole in it. Every frequency-domain stage below refuses it, and
        # dropping the column instead would change the vector's length and silently misalign it
        # against every stored fingerprint.
        return None

    series, _, coverage = uniform_resample(window.t_us, block, config.fs)
    if series.shape[0] < 32:
        return None
    if not coverage.acceptable(
        max_gap_s=config.max_gap_s, max_interpolated=config.max_interpolated
    ):
        return None

    flat = detrend(series.astype(np.float64))

    profile = _band_profile(flat, config)
    if profile is None:
        return None
    coherence = _coherence(flat, config)
    if coherence is None:
        return None

    vector = np.concatenate(
        [
            profile * (1.0 - config.coherence_weight),
            coherence * config.coherence_weight,
        ]
    )
    return _normalize(vector)


def _band_profile(flat: np.ndarray, config: ZoneConfig) -> np.ndarray | None:
    """Motion-band power per subcarrier, in dB about its own mean. The discriminative block.

    The dB and the centring are both load-bearing, and leaving either out was measured to
    collapse the feature. Raw band power is non-negative, so every profile lives in the positive
    orthant and two quite different rooms come out at a cosine distance of 0.03 while an empty
    room sits at 0.25 — the classifier can tell movement from stillness and nothing finer.
    Taking the log turns the multiplicative differences the channel actually produces into
    additive ones, and subtracting the mean throws away the overall level, which is a property of
    how vigorously the person moved rather than of where they were. What is left is the shape:
    which subcarriers see more motion than average, which is the Fresnel selectivity that varies
    with position.
    """
    freqs, psd = welch_psd(flat, config.fs, nperseg=_nperseg(config, flat.shape[0]))
    power = np.asarray(band_power(freqs, psd, config.band[0], config.band[1]), dtype=np.float64)
    if power.ndim == 0 or power.size < 2:
        return None

    scale = float(np.median(power))
    if not np.isfinite(scale) or scale <= 0.0:
        return None
    # Floored relative to the window's own median rather than at an absolute value, so the
    # dynamic range admitted is the same whatever the amplitude units happen to be.
    db = 10.0 * np.log10(np.maximum(power, scale * 1e-4) / scale)
    return _normalize(db - db.mean())


def _coherence(flat: np.ndarray, config: ZoneConfig) -> np.ndarray | None:
    """Normalized leading eigenvalues of the z-scored window.

    How *concentrated* the perturbation is across subcarriers. A body crossing the direct path
    moves everything together and puts nearly all the energy in the first component; one
    disturbing a single weak scatterer spreads it. That is a different axis from the profile —
    two zones can have similar profile shapes and quite different coherence — which is why it is
    worth the eight extra numbers.
    """
    std = flat.std(axis=0)
    good = std > 1e-12
    if not good.any():
        return None
    z = np.zeros_like(flat)
    z[:, good] = flat[:, good] / std[good]

    try:
        sv = np.linalg.svd(z, compute_uv=False)
    except np.linalg.LinAlgError:
        return None
    if sv.size == 0:
        return None

    energy = np.square(sv)
    total = float(energy.sum())
    if not np.isfinite(total) or total <= 0.0:
        return None

    # Padded to a fixed length so the vector's dimension depends only on the binding, never on
    # how many frames happened to arrive.
    out = np.full(config.coherence_k, 1e-6, dtype=np.float64)
    take = min(config.coherence_k, energy.size)
    out[:take] = np.maximum(energy[:take] / total, 1e-6)
    # In dB about its own mean, for the same reason the profile is — the eigenvalue fractions
    # are non-negative and the first one dominates in every room, so compared raw they are
    # nearly the same vector everywhere and contribute nothing but a constant.
    db = 10.0 * np.log10(out)
    return _normalize(db - db.mean())


def _normalize(vector: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 1e-12:
        return None
    return vector / norm


def sub_windows(window: Window, config: ZoneConfig) -> list[Window]:
    """Split a captured example into overlapping analysis windows, oldest first.

    Selected on the device clock rather than by frame count, for the reason `FrameRing.seconds`
    is: an example that lost frames should yield fewer windows, not the same number of shorter
    ones wearing the right length.
    """
    if len(window) < 16:
        return []
    t = window.t_us
    span_us = int(config.window_s * 1e6)
    hop_us = int(max(config.hop_s, 0.5) * 1e6)
    if t[-1] - t[0] < span_us:
        return [window]

    def slice_at(start: int) -> Window | None:
        lo = int(np.searchsorted(t, start, side="left"))
        hi = int(np.searchsorted(t, start + span_us, side="right"))
        if hi - lo < 16:
            return None
        return Window(
            t_us=t[lo:hi],
            amp=window.amp[lo:hi],
            agc=window.agc[lo:hi],
            rssi=window.rssi[lo:hi],
            mask=window.mask,
        )

    out: list[Window] = []
    start = int(t[0])
    last_start = start
    while start + span_us <= int(t[-1]):
        piece = slice_at(start)
        if piece is not None:
            out.append(piece)
            last_start = start
        start += hop_us

    # The tail, anchored at the end rather than the start. Without this a 16 s example at an 8 s
    # window and a 4 s hop yields two windows instead of three, because the third would have had
    # to start at 8.00 s in a recording 15.99 s long. Losing a third of the vectors — and with
    # them a third of the evidence the zone's own spread is estimated from — to a rounding error
    # is not a trade worth making. Skipped when it would nearly duplicate the last one.
    tail_start = int(t[-1]) - span_us
    if out and tail_start - last_start >= hop_us // 2:
        piece = slice_at(tail_start)
        if piece is not None:
            out.append(piece)
    return out


def build_gate(
    mean_profiles: np.ndarray, mask: np.ndarray, config: ZoneConfig
) -> np.ndarray:
    """The fixed subcarrier set, from the median mean-amplitude across every stored example.

    Same principle as `magnitude_gate` — low mean magnitude is the deep-fade signature and
    belongs in an exclusion filter — but computed once over all the examples and then frozen
    into the model, so both sides of every comparison are indexed identically.
    """
    valid = np.flatnonzero(mask)
    if valid.size == 0 or mean_profiles.size == 0:
        return np.array([], dtype=int)

    block = np.atleast_2d(np.asarray(mean_profiles, dtype=np.float64))[:, valid]
    usable = np.isfinite(block).all(axis=0)
    if not usable.any():
        return np.array([], dtype=int)

    means = np.full(valid.size, -np.inf)
    means[usable] = np.median(block[:, usable], axis=0)
    threshold = float(np.quantile(means[usable], float(np.clip(config.gate_quantile, 0.0, 0.95))))
    return valid[usable & (means >= threshold)]


# -- the model -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SampleVectors:
    """One recorded example, reduced to the vectors it yields."""

    sample_id: str
    zone_id: str
    vectors: np.ndarray  # (k, dim)


@dataclass(frozen=True, slots=True)
class ZoneMatch:
    state: str  # matched | unknown | ambiguous
    zone_id: str | None
    distance: float
    # How far ahead of the runner-up the winner was, or None when there is no runner-up because
    # only one zone has been taught. None rather than infinity: this ends up in a JSON payload,
    # and `json.dumps` writes a float infinity as the literal `Infinity`, which is not valid
    # JSON and which `JSON.parse` refuses — taking the whole metrics message down with it, for
    # every node, on the one configuration a user necessarily passes through on their way to a
    # second zone.
    margin: float | None
    scores: tuple[tuple[str, float], ...]

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "zone_id": self.zone_id,
            "distance": round(self.distance, 4),
            "margin": round(self.margin, 4) if self.margin is not None else None,
            "scores": [{"zone_id": z, "distance": round(d, 4)} for z, d in self.scores],
        }


@dataclass(frozen=True, slots=True)
class ZoneModel:
    """An immutable snapshot of everything needed to classify one node's windows.

    Frozen on purpose. `compute_metrics` runs in a worker thread and reaches for this the way it
    reaches for a `History` snapshot — the store swaps in a whole new model when a sample is
    added or deleted, so the worker can never see a half-rebuilt one.
    """

    binding: Binding | None = None
    gate: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    zone_ids: tuple[str, ...] = ()
    centroids: np.ndarray = field(default_factory=lambda: np.zeros((0, 0)))
    # Where each zone's own examples sit relative to its centroid, kept as the two numbers the
    # threshold is derived from rather than as the threshold itself. That is what lets the
    # rejection sliders in the UI take effect immediately: `config` is the same mutable instance
    # the hub edits, so moving one re-derives every threshold without reloading a single array
    # from disk. Same arrangement as `PresenceDetector.config`.
    spread_centre: np.ndarray = field(default_factory=lambda: np.zeros(0))
    spread_sigma: np.ndarray = field(default_factory=lambda: np.zeros(0))
    counts: tuple[int, ...] = ()
    # Median distance between centroids: how far apart this room's zones actually are. Both
    # rejection thresholds are fractions of it. 0.0 when there is only one zone.
    separation: float = 0.0
    config: ZoneConfig = field(default_factory=ZoneConfig)

    @property
    def thresholds(self) -> np.ndarray:
        """Per-zone rejection radius, in cosine distance."""
        floor = (
            self.config.reject_separation_frac * self.separation
            if self.separation > 0.0
            else self.config.reject_floor
        )
        return np.maximum(
            self.spread_centre + self.config.reject_sigma * self.spread_sigma, floor
        )

    @property
    def trained(self) -> bool:
        """Has centroids to compare against. Deliberately says nothing about the gate: the
        cross-validation builds models over vectors that were extracted earlier and has no gate
        to give them. Whoever needs to *extract* features checks the gate itself."""
        return len(self.zone_ids) > 0

    def match(self, features: np.ndarray) -> ZoneMatch | None:
        """Nearest centroid by cosine distance, with rejection.

        Both the vector and the centroids are unit length, so the cosine distance is just
        `1 - dot`. Two separate ways to decline: too far from everything (`unknown`), or too
        close to call between the best two (`ambiguous`). They are different problems — the
        first wants another example, the second wants the node moved — so they are reported
        differently.
        """
        if not self.trained or features.shape[0] != self.centroids.shape[1]:
            return None

        thresholds = self.thresholds
        distances = 1.0 - self.centroids @ features
        order = np.argsort(distances)
        best = int(order[0])
        best_d = float(distances[best])
        margin = float(distances[order[1]]) - best_d if order.size > 1 else None

        scores = tuple((self.zone_ids[i], float(distances[i])) for i in order)

        if best_d > float(thresholds[best]):
            return ZoneMatch("unknown", None, best_d, margin, scores)
        if margin is not None and margin < self.margin_threshold:
            return ZoneMatch("ambiguous", None, best_d, margin, scores)
        return ZoneMatch("matched", self.zone_ids[best], best_d, margin, scores)

    @property
    def margin_threshold(self) -> float:
        return max(self.config.margin_frac * self.separation, self.config.min_margin)


def build_model(
    samples: list[SampleVectors],
    *,
    binding: Binding | None,
    gate: np.ndarray,
    config: ZoneConfig,
) -> ZoneModel:
    """Centroids and rejection thresholds from the examples on hand."""
    grouped: dict[str, list[np.ndarray]] = {}
    for sample in samples:
        if sample.vectors.size:
            grouped.setdefault(sample.zone_id, []).append(sample.vectors)

    zone_ids: list[str] = []
    centroids: list[np.ndarray] = []
    spreads: list[np.ndarray] = []
    counts: list[int] = []

    for zone_id in sorted(grouped):
        stack = np.vstack(grouped[zone_id])
        centroid = _normalize(stack.mean(axis=0))
        if centroid is None:
            continue
        zone_ids.append(zone_id)
        centroids.append(centroid)
        spreads.append(1.0 - stack @ centroid)
        counts.append(int(stack.shape[0]))

    if not zone_ids:
        return ZoneModel(binding=binding, gate=gate, config=config)

    matrix = np.vstack(centroids)

    # Median and robust spread of each zone's own examples about its own centroid. A zone with a
    # single example has no spread at all, which is why `ZoneModel.thresholds` floors the result
    # — and why the cross-validation reports that example as `only-sample` rather than pretending
    # to have scored it.
    return ZoneModel(
        binding=binding,
        gate=gate,
        zone_ids=tuple(zone_ids),
        centroids=matrix,
        spread_centre=np.array([float(np.median(s)) for s in spreads]),
        spread_sigma=np.array(
            [float(robust_std(s)) if s.size > 2 else 0.0 for s in spreads]
        ),
        counts=tuple(counts),
        separation=_separation(matrix),
        config=config,
    )


def _separation(centroids: np.ndarray) -> float:
    """Median cosine distance between distinct centroids. 0.0 for a single zone."""
    if centroids.shape[0] < 2:
        return 0.0
    distances = 1.0 - centroids @ centroids.T
    upper = distances[np.triu_indices(centroids.shape[0], k=1)]
    return float(np.median(upper)) if upper.size else 0.0


# -- the live classifier -------------------------------------------------------------------


class ZoneClassifier:
    """One per node. Stateful only in its vote history."""

    def __init__(self, config: ZoneConfig | None = None) -> None:
        self.config = config or ZoneConfig()
        self._votes: deque[tuple[int, str]] = deque(maxlen=256)
        self._published: str | None = None

    def reset(self) -> None:
        """Forget the vote history. Called wherever the node's history is thrown away — the
        votes describe a link that has gone, and holding them would let a stale zone survive
        several seconds into a new one."""
        self._votes.clear()
        self._published = None

    def update(
        self, history: History, model: ZoneModel, *, moving: bool, binding: Binding | None
    ) -> dict | None:
        """One classification, or a stated reason there is none.

        `moving` comes from the presence detector. Classifying a still room is not a harmless
        no-op: the profile is thermal noise, it is as unit-length as any other vector, and the
        nearest centroid to it is a perfectly definite zone. The gate is the whole reason the
        readout can be trusted.
        """
        if not model.trained or model.gate.size == 0:
            return {"state": "untrained", "reason": "no zones have been taught yet"}

        if model.binding is not None and binding is not None:
            reason = model.binding.differs_from(binding)
            if reason is not None:
                self.reset()
                return {"state": "stale", "reason": reason}

        if not moving:
            self._votes.clear()
            self._published = None
            return {"state": "idle", "reason": "nothing is moving"}

        window = history.seconds(self.config.window_s)
        if len(window) < 16:
            return {"state": "idle", "reason": "not enough history yet"}

        features = zone_features(window, model.gate, self.config)
        if features is None:
            return {
                "state": "idle",
                "reason": "the window had a hole in it too big to fingerprint across",
            }

        match = model.match(features)
        if match is None:
            return {"state": "stale", "reason": "the model does not fit this node's frames"}

        now_us = int(window.t_us[-1])
        label = self._vote(now_us, match.zone_id or match.state)

        out = match.as_dict()
        if label is None:
            out["state"] = "settling"
            out["zone_id"] = None
        elif label in ("unknown", "ambiguous"):
            out["state"] = label
            out["zone_id"] = None
        else:
            out["state"] = "matched"
            out["zone_id"] = label
        return out

    def _vote(self, now_us: int, label: str) -> str | None:
        """Majority over the vote window. None while nothing has a majority yet."""
        self._votes.append((now_us, label))
        cutoff = now_us - int(self.config.vote_s * 1e6)
        while self._votes and self._votes[0][0] < cutoff:
            self._votes.popleft()

        counts = Counter(label for _, label in self._votes)
        winner, count = counts.most_common(1)[0]
        # A strict majority, and never on the strength of one window. An exact tie would
        # otherwise publish whichever label `most_common` happened to order first, which is a
        # coin flip wearing the appearance of a decision.
        if count * 2 > len(self._votes) and count >= 2:
            self._published = winner
        return self._published


# -- quality feedback ------------------------------------------------------------------------


def cross_validate(samples: list[SampleVectors], config: ZoneConfig) -> dict:
    """Leave-one-out over the stored examples.

    This is what makes deleting an example a decision rather than a guess. A per-sample verdict
    says which zone it *would* be classified as with itself held out, so an example recorded
    while standing in the wrong place, or while barely moving, is visible as itself rather than
    as a vague sense that the feature is not working.

    The gate is held fixed across folds instead of being rebuilt per fold. Rebuilding it would
    be more rigorous and would change nothing: it is a quantile of a median over every example,
    and dropping one of six does not move it.
    """
    per_zone: dict[str, list[str]] = {}
    for sample in samples:
        per_zone.setdefault(sample.zone_id, []).append(sample.sample_id)

    results: list[dict] = []
    confusion: dict[str, Counter] = {zone: Counter() for zone in per_zone}

    for held in samples:
        if held.vectors.size == 0:
            results.append(
                {"sample_id": held.sample_id, "zone_id": held.zone_id, "verdict": "unusable"}
            )
            continue
        if len(per_zone[held.zone_id]) < 2:
            # Held out, its zone has no centroid left, so it could never come back correct.
            # Reporting that as a failure would be a lie about the example.
            results.append(
                {
                    "sample_id": held.sample_id,
                    "zone_id": held.zone_id,
                    "verdict": "only-sample",
                    "predicted": None,
                }
            )
            continue

        rest = [s for s in samples if s.sample_id != held.sample_id]
        # No gate: these vectors were extracted when the sample was stored, so there is nothing
        # left to gate. `ZoneModel.trained` is written not to care.
        model = build_model(rest, binding=None, gate=np.array([], dtype=int), config=config)
        labels = []
        for vector in held.vectors:
            match = model.match(vector)
            labels.append(match.zone_id or match.state if match is not None else "unusable")
        predicted = Counter(labels).most_common(1)[0][0]

        confusion[held.zone_id][predicted] += 1
        results.append(
            {
                "sample_id": held.sample_id,
                "zone_id": held.zone_id,
                "verdict": "correct" if predicted == held.zone_id else "wrong",
                "predicted": predicted if predicted in per_zone else None,
                "predicted_state": predicted,
            }
        )

    accuracy = {}
    for zone_id, ids in per_zone.items():
        scored = [
            r
            for r in results
            if r["zone_id"] == zone_id and r["verdict"] in ("correct", "wrong")
        ]
        correct = sum(1 for r in scored if r["verdict"] == "correct")
        accuracy[zone_id] = {
            "samples": len(ids),
            "scored": len(scored),
            "correct": correct,
            "accuracy": round(correct / len(scored), 3) if scored else None,
        }

    return {
        "samples": results,
        "zones": accuracy,
        "confusion": {zone: dict(counter) for zone, counter in confusion.items()},
        "indistinguishable": _indistinguishable(confusion),
    }


def _indistinguishable(confusion: dict[str, Counter]) -> list[dict]:
    """Zone pairs that get mistaken for each other in both directions.

    One-directional confusion is usually a thin example set. Confusion *both* ways means the two
    places look the same down this link, which is not something more recording will fix — the
    answer is to move the node, pick a different access point, or accept that they are one zone.
    """
    out = []
    zones = sorted(confusion)
    for i, a in enumerate(zones):
        for b in zones[i + 1 :]:
            a_to_b = confusion[a].get(b, 0)
            b_to_a = confusion[b].get(a, 0)
            if a_to_b > 0 and b_to_a > 0:
                out.append({"zones": [a, b], "count": a_to_b + b_to_a})
    return sorted(out, key=lambda item: -item["count"])
