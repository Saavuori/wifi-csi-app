"""Phase 4: motion energy and a binary "someone is here".

The detector is deliberately not clever. Sliding-window variance against a calibrated empty-room
baseline, hysteresis so the state does not chatter at the threshold, and a debounce so a single
noisy window cannot flip it. The hard parts are elsewhere: picking the subcarriers (selection.py)
and removing the AGC steps that would otherwise dominate the variance (preprocess.py).

On baseline drift: measured channel coherence time falls from roughly 4.6 s with everything
stationary to about 1.5 s once anything in the environment is moving, and the empty-room
baseline drifts over hours regardless. So the baseline is refreshed continuously while the room
reads as empty, rather than being measured once at startup and trusted forever.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import numpy as np

from ..ring import History, Window
from .selection import SelectionConfig, magnitude_gate
from .util import robust_std

# Fraction of the calibration period after which a provisional baseline is formed and the
# motion index starts being scored. Everything before this only accumulates.
_PROVISIONAL_AT = 0.4


class State(enum.StrEnum):
    CALIBRATING = "calibrating"
    ABSENT = "absent"
    PRESENT = "present"


@dataclass(slots=True)
class PresenceConfig:
    window_s: float = 2.0
    calibration_s: float = 30.0

    # Thresholds are in robust standard deviations of the motion index as measured during
    # calibration, so they mean the same thing in a quiet room and a noisy one.
    enter_sigma: float = 6.0
    exit_sigma: float = 2.5
    debounce_s: float = 1.5

    use_pca: bool = True
    n_components: int = 3

    selection: SelectionConfig = field(default_factory=SelectionConfig)

    # While the room reads as empty, fold the current variance into the baseline with this time
    # constant (seconds). Long enough that a motionless person does not get absorbed into the
    # baseline within a sitting; short enough to track the hours-scale drift.
    baseline_tau_s: float = 600.0


@dataclass(slots=True)
class PresenceResult:
    state: State
    motion: float  # motion index, ~1.0 in a calibrated empty room
    threshold_enter: float
    threshold_exit: float
    calibration_progress: float  # 0..1
    selected: list[int]
    since_s: float  # how long the current state has held

    def as_dict(self) -> dict:
        return {
            "state": str(self.state),
            "motion": round(self.motion, 4),
            "enter": round(self.threshold_enter, 4),
            "exit": round(self.threshold_exit, 4),
            "calibration": round(self.calibration_progress, 3),
            "selected": self.selected,
            "since_s": round(self.since_s, 1),
        }


class PresenceDetector:
    """One per node. `update` is cheap enough to call at the metrics rate (10 Hz)."""

    def __init__(self, config: PresenceConfig | None = None) -> None:
        self.config = config or PresenceConfig()
        self.state = State.CALIBRATING
        self.baseline_var: np.ndarray | None = None
        self._calib_samples: list[np.ndarray] = []
        self._calib_motion: list[float] = []
        self._calib_started_us: int | None = None
        self._enter = 4.0
        self._exit = 2.0
        self._pending: State | None = None
        self._pending_since_us: int = 0
        self._state_since_us: int = 0
        self._selected: np.ndarray = np.array([], dtype=int)
        self._last_motion = 1.0

    def recalibrate(self) -> None:
        """Throw away the baseline and start over. The room must be empty when this is called —
        that is the one thing the detector cannot check for itself."""
        self.state = State.CALIBRATING
        self.baseline_var = None
        self._calib_samples.clear()
        self._calib_motion.clear()
        self._calib_started_us = None
        self._pending = None

    def update(self, history: History) -> PresenceResult | None:
        cfg = self.config
        window = history.seconds(cfg.window_s)
        if len(window) < 8:
            return None

        now_us = int(window.t_us[-1])
        var = self._window_variance(window)
        if var is None:
            return None

        if self.state is State.CALIBRATING:
            return self._calibrate(window, var, now_us)

        motion = self._motion_index(window, var)
        self._last_motion = motion
        self._step_state(motion, now_us)
        self._maybe_drift_baseline(var)

        return PresenceResult(
            state=self.state,
            motion=motion,
            threshold_enter=self._enter,
            threshold_exit=self._exit,
            calibration_progress=1.0,
            selected=self._selected.tolist(),
            since_s=(now_us - self._state_since_us) / 1e6,
        )

    # -- internals ----------------------------------------------------------------------

    def _window_variance(self, window: Window) -> np.ndarray | None:
        """Per-subcarrier variance over the window, on the full n_sub axis. NaN where unusable.

        Frames flagged as AGC steps are excluded rather than merely normalized. Normalization
        removes the *level* shift; what remains at the transition is a one-sample discontinuity
        that still contributes to variance, and that is precisely the false positive we are
        trying to avoid.
        """
        keep = ~window.agc
        if keep.sum() < 8:
            keep = np.ones(len(window), dtype=bool)

        amp = window.amp[keep]
        var = np.full(window.mask.size, np.nan, dtype=np.float64)
        valid = np.flatnonzero(window.mask)
        if valid.size == 0:
            return None
        var[valid] = np.nanvar(amp[:, valid], axis=0)
        return var

    def _calibrate(
        self, window: Window, var: np.ndarray, now_us: int
    ) -> PresenceResult:
        """Two stages inside one calibration period.

        Stage one accumulates variance vectors to form a provisional baseline. Stage two keeps
        accumulating, but also scores each window against that provisional baseline — which is
        what gives us a distribution of the motion index *in an empty room*, and therefore
        thresholds expressed in units of that room's own noise.

        Scoring only after a provisional baseline exists avoids the circularity of measuring an
        index against a baseline that the same sample is still defining.
        """
        cfg = self.config
        if self._calib_started_us is None:
            self._calib_started_us = now_us
            self._state_since_us = now_us
            self._selected = self._select(window)

        self._calib_samples.append(var)
        elapsed_s = (now_us - self._calib_started_us) / 1e6
        progress = min(1.0, elapsed_s / cfg.calibration_s)

        if progress >= _PROVISIONAL_AT and len(self._calib_samples) >= 4:
            if self.baseline_var is None:
                self.baseline_var = _baseline_from(self._calib_samples)
                self._selected = self._select(window)
            m = self._motion_index(window, var)
            self._last_motion = m
            if np.isfinite(m):
                self._calib_motion.append(m)

        if progress >= 1.0 and len(self._calib_motion) >= 4:
            self.baseline_var = _baseline_from(self._calib_samples)
            motions = np.array(self._calib_motion)
            centre = float(np.median(motions))
            spread = float(robust_std(motions))
            # Floor the spread. A pathologically quiet calibration yields spread ~0 and
            # thresholds that sit on top of the baseline, which makes everything a detection.
            spread = max(spread, 0.15 * max(centre, 1e-6))

            self._enter = centre + cfg.enter_sigma * spread
            self._exit = centre + cfg.exit_sigma * spread
            self.state = State.ABSENT
            self._state_since_us = now_us
            self._calib_samples.clear()
            self._calib_motion.clear()

        return PresenceResult(
            state=State.CALIBRATING,
            motion=self._last_motion,
            threshold_enter=self._enter,
            threshold_exit=self._exit,
            calibration_progress=progress,
            selected=self._selected.tolist(),
            since_s=elapsed_s,
        )

    def _select(self, window: Window) -> np.ndarray:
        """Gate on magnitude and keep what survives.

        At calibration time there is no occupied recording to rank against, so the
        discriminability term of selection.py is unavailable and the gate does all the work.
        Once labelled recordings exist, `apply_selection` replaces this with the real ranking.
        """
        cfg = self.config
        kept, _ = magnitude_gate(window, cfg.selection.gate_quantile)
        return kept

    def apply_selection(self, indices: np.ndarray) -> None:
        """Override the gated set with a ranked one, e.g. from `selection.rank_for_motion`
        computed over labelled `empty-room` and `walking` recordings."""
        self._selected = np.asarray(indices, dtype=int)

    def _motion_index(self, window: Window, var: np.ndarray) -> float:
        """How far the current window sits from the empty-room baseline. ~1.0 when empty.

        Without PCA: the median across selected subcarriers of var_now / var_baseline. Median
        rather than mean, because one subcarrier going haywire is a hardware event, not a
        person.

        With PCA: each selected subcarrier is scaled by its baseline standard deviation, so in
        an empty room the window matrix has roughly unit variance in every column and no
        particular correlation structure — its leading eigenvalues are all near 1. Motion is
        correlated across subcarriers (one body perturbs many multipath components at once), so
        it piles variance into the first few components while thermal noise stays spread across
        all of them. Summing the top components and dividing by their count therefore gives a
        quantity that is ~1.0 when empty and grows with *structured* energy specifically. Both
        forms share the same baseline value, so the calibrated thresholds mean the same thing
        either way.
        """
        if self.baseline_var is None:
            return 1.0
        idx = self._selected if self._selected.size else np.flatnonzero(np.isfinite(var))
        if idx.size == 0:
            return 1.0

        base = self.baseline_var[idx]
        cur = var[idx]
        good = np.isfinite(cur) & np.isfinite(base) & (base > 0)
        if not good.any():
            return 1.0
        idx = idx[good]

        if self.config.use_pca and idx.size >= 4 and len(window) >= 16:
            energy = self._pca_energy(window, idx)
            if energy is not None:
                return energy

        return float(np.median(var[idx] / self.baseline_var[idx]))

    def _pca_energy(self, window: Window, idx: np.ndarray) -> float | None:
        assert self.baseline_var is not None
        keep = ~window.agc
        if keep.sum() < 16:
            keep = np.ones(len(window), dtype=bool)

        x = window.amp[keep][:, idx].astype(np.float64)
        if not np.isfinite(x).all():
            return None

        scale = np.sqrt(self.baseline_var[idx])
        scale = np.where(scale > 0, scale, np.nan)
        z = (x - x.mean(axis=0)) / scale
        if not np.isfinite(z).all():
            return None

        n = z.shape[0]
        try:
            sv = np.linalg.svd(z, compute_uv=False)
        except np.linalg.LinAlgError:
            return None

        k = max(1, min(self.config.n_components, sv.size))
        # Eigenvalues of the covariance matrix are sv^2 / (n - 1); in an empty room each is ~1
        # by construction of the scaling above, so the mean of the top k is ~1 too.
        return float(np.sum(sv[:k] ** 2) / (n - 1) / k)

    def _step_state(self, motion: float, now_us: int) -> None:
        cfg = self.config
        target = self.state
        if self.state is State.ABSENT and motion >= self._enter:
            target = State.PRESENT
        elif self.state is State.PRESENT and motion <= self._exit:
            target = State.ABSENT

        if target is self.state:
            self._pending = None
            return

        if self._pending is not target:
            self._pending = target
            self._pending_since_us = now_us
            return

        if (now_us - self._pending_since_us) / 1e6 >= cfg.debounce_s:
            self.state = target
            self._state_since_us = now_us
            self._pending = None

    def _maybe_drift_baseline(self, var: np.ndarray) -> None:
        """Slowly track the empty-room baseline while the room reads as empty."""
        if self.state is not State.ABSENT or self.baseline_var is None:
            return
        # One update per call; the hub calls at a known rate, so convert tau to a per-call
        # coefficient using the presence window length as the effective step.
        alpha = min(1.0, self.config.window_s / max(self.config.baseline_tau_s, 1.0))
        good = np.isfinite(var) & np.isfinite(self.baseline_var)
        self.baseline_var[good] += alpha * (var[good] - self.baseline_var[good])


def _baseline_from(samples: list[np.ndarray]) -> np.ndarray:
    """Per-subcarrier baseline variance, as the median across calibration windows.

    Median rather than mean: if someone walked past during the 30 s the user was told to stay
    out of the room, the median shrugs it off while the mean quietly doubles the baseline and
    desensitizes the detector for the rest of the session.

    Masked subcarriers stay NaN, and are excluded from the reduction rather than passed to
    nanmedian as all-NaN columns — which is the same answer, without a warning per window.
    """
    stack = np.vstack(samples)
    out = np.full(stack.shape[1], np.nan)
    usable = np.flatnonzero(np.isfinite(stack).any(axis=0))
    if usable.size:
        out[usable] = np.nanmedian(stack[:, usable], axis=0)
    return out
