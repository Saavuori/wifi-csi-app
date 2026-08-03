"""Per-frame preprocessing: masking, AGC removal, impulse rejection.

Everything here runs identically on live and replayed frames — that is the whole point of
putting it on the server rather than in the browser. A recording is only a stand-in for the
room if it goes through the same arithmetic.

The AGC problem, in one paragraph: the receiver's automatic gain control adjusts as it likes,
and when it steps, every subcarrier's amplitude jumps by the same factor at once. To a variance
detector that is indistinguishable from someone walking through the room, and it happens often
enough to make raw variance useless. Two defenses are applied here, and they are independent —
normalization removes the step, and the uniformity test flags the frames where it happened so
downstream can also decline to integrate across the transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from ..protocol import Frame
from .subcarriers import valid_mask

NormMode = Literal["hybrid", "rssi", "rms", "none"]

# Output amplitudes are scaled so a frame at RSSI_REF_DBM lands at NOMINAL_RMS. Both constants
# are cosmetic — they exist so normalized amplitude sits in the same numeric range as raw int8
# CSI and the waterfall's default colour scale works without retuning.
RSSI_REF_DBM = -50.0
NOMINAL_RMS = 40.0


@dataclass(slots=True)
class PreprocessConfig:
    norm_mode: NormMode = "hybrid"
    drop_pilots: bool = True
    # Drop k = +/-1. Off by default because the standard says those carry data and on an ESP32
    # they do; on the Pi's BCM43455 they carry DC leakage at ~8x the median amplitude. See
    # valid_mask().
    drop_dc_adjacent: bool = False

    hampel_enabled: bool = True
    hampel_window: int = 5  # half-width, in subcarriers
    hampel_n_sigma: float = 3.0

    # An AGC step is a large, *uniform* change. Both conditions must hold: a large change that
    # is frequency-selective is a person, and a uniform change that is small is noise.
    agc_step_db: float = 1.0
    agc_uniformity: float = 0.4  # robust spread / |median shift|, below this means uniform

    # Time constant for the smoothed RSSI trend in hybrid mode, in frames. This must be *slow*,
    # and the reason is not obvious: the EMA does not remove RSSI's 1 dB quantization noise, it
    # low-pass filters it, which concentrates what remains at frequencies below the EMA corner.
    # A 3 s time constant puts that corner at 0.33 Hz — directly inside the respiration band —
    # and re-applying the trend then multiplies every subcarrier by a common-mode wobble sitting
    # exactly where the breathing peak is. Measured against synthetic 14 BPM scenes, moving the
    # time constant from 3 s to 30 s takes breathing MAE from 1.70 to 0.29 breaths/min. At 80 Hz
    # 2400 frames is 30 s, a 0.005 Hz corner, comfortably below anything we analyse.
    rssi_ema_frames: int = 2400


@dataclass(slots=True)
class Processed:
    """One preprocessed frame. `amp` is NaN wherever the subcarrier is masked out."""

    frame: Frame
    amp: np.ndarray  # float32, shape (n_sub,), NaN at masked positions
    mask: np.ndarray  # bool, shape (n_sub,)
    agc_step: bool
    gain_db: float  # estimated AGC gain removed, dB. Diagnostic only.
    raw_rms: float

    @property
    def valid(self) -> np.ndarray:
        """Amplitudes of usable subcarriers only, shape (n_valid,). No NaNs."""
        return self.amp[self.mask]


class Preprocessor:
    """Stateful, one instance per node. Not thread-safe; the hub owns it on one task."""

    def __init__(self, config: PreprocessConfig | None = None) -> None:
        self.config = config or PreprocessConfig()
        self._prev_log: np.ndarray | None = None
        self._prev_n_sub: int = 0
        self._rssi_ema: float | None = None
        self.agc_steps = 0
        self.frames = 0

    def reset(self) -> None:
        """Drop history. Called when a node reboots or a replay seeks, so that the frame across
        the discontinuity is not reported as a gain event."""
        self._prev_log = None
        self._prev_n_sub = 0
        self._rssi_ema = None

    def process(self, frame: Frame) -> Processed:
        cfg = self.config
        mask = valid_mask(
            frame.n_sub,
            drop_pilots=cfg.drop_pilots,
            drop_dc_adjacent=cfg.drop_dc_adjacent,
        )
        amp = frame.amplitude()

        if frame.n_sub != self._prev_n_sub:
            # Bandwidth changed mid-stream (HT20 <-> HT40). Nothing carries over.
            self.reset()
            self._prev_n_sub = frame.n_sub

        agc_step, log_amp = self._detect_agc_step(amp, mask)
        self._prev_log = log_amp

        raw_rms = float(np.sqrt(np.mean(np.square(amp[mask])))) if mask.any() else 0.0
        amp, gain_db = self._normalize(amp, mask, frame.rssi, raw_rms)

        if cfg.hampel_enabled:
            amp = hampel(amp, mask, cfg.hampel_window, cfg.hampel_n_sigma)

        out = np.full(frame.n_sub, np.nan, dtype=np.float32)
        out[mask] = amp[mask]

        self.frames += 1
        if agc_step:
            self.agc_steps += 1

        return Processed(
            frame=frame,
            amp=out,
            mask=mask,
            agc_step=agc_step,
            gain_db=gain_db,
            raw_rms=raw_rms,
        )

    # -- internals ----------------------------------------------------------------------

    def _detect_agc_step(self, amp: np.ndarray, mask: np.ndarray) -> tuple[bool, np.ndarray]:
        """Uniformity test. Returns (is_step, log-amplitude to carry forward).

        Real motion is frequency-selective: a moving body changes some subcarriers far more
        than others, because the multipath components it disturbs land on particular
        frequencies. An AGC step multiplies every subcarrier by the same number. So we look at
        the per-subcarrier log ratio to the previous frame and ask how *spread out* it is: a
        large median shift with a small spread is the receiver changing gain, not the room
        changing shape.
        """
        cfg = self.config
        log_amp = np.log(np.maximum(amp, 1e-3, dtype=np.float64))

        prev = self._prev_log
        if prev is None or prev.shape != log_amp.shape or not mask.any():
            return False, log_amp

        delta = (log_amp - prev)[mask]
        median = float(np.median(delta))
        shift_db = abs(median) * 20.0 / np.log(10.0)
        if shift_db < cfg.agc_step_db:
            return False, log_amp

        spread = float(np.median(np.abs(delta - median)))
        uniform = spread < cfg.agc_uniformity * abs(median)
        return uniform, log_amp

    def _normalize(
        self, amp: np.ndarray, mask: np.ndarray, rssi: int, raw_rms: float
    ) -> tuple[np.ndarray, float]:
        """Remove the AGC gain. Returns (amplitude, estimated gain removed in dB)."""
        cfg = self.config
        if cfg.norm_mode == "none" or raw_rms <= 0.0:
            return amp, 0.0

        # RSSI is the radio's own estimate of received power *after* it has compensated for
        # its gain setting, so it tracks the true field. Raw CSI amplitude is the true field
        # times the gain. Their ratio is therefore the gain, and dividing it out is the fix.
        if self._rssi_ema is None:
            self._rssi_ema = float(rssi)
        else:
            a = 2.0 / (cfg.rssi_ema_frames + 1.0)
            self._rssi_ema += a * (float(rssi) - self._rssi_ema)

        if cfg.norm_mode == "rms":
            # Pure per-frame RMS normalization. Removes the AGC step exactly and adds no noise,
            # but also flattens genuine broadband power changes (someone standing in the direct
            # path). Frequency-selective structure — which is what carries motion and breathing
            # — survives untouched.
            target_dbm = RSSI_REF_DBM
        elif cfg.norm_mode == "rssi":
            # As specified in the plan: match reported per-frame power. Faithful to real power
            # changes, but RSSI is quantized to 1 dBm, so every RSSI tick injects a ~12%
            # amplitude step on every subcarrier — the same artifact we are trying to remove,
            # a little smaller.
            target_dbm = float(rssi)
        else:  # hybrid, the default
            # Divide by the measured RMS (exact AGC removal, no quantization) and re-apply a
            # smoothed RSSI trend, so slow genuine changes in received power are preserved
            # while the 1 dB stair-step is averaged away.
            target_dbm = self._rssi_ema

        scale = NOMINAL_RMS * (10.0 ** ((target_dbm - RSSI_REF_DBM) / 20.0)) / raw_rms
        gain_db = -20.0 * np.log10(max(scale, 1e-12))
        return (amp * np.float32(scale)), float(gain_db)


def hampel(
    amp: np.ndarray, mask: np.ndarray, window: int, n_sigma: float
) -> np.ndarray:
    """Hampel filter across the subcarrier axis of a single frame.

    Replaces points more than `n_sigma` robust standard deviations from the local median with
    that median. Applied across frequency rather than across time on purpose: it is stateless,
    adds no latency, and the impulses we are chasing here (a single bin blown out by a
    collision or an ADC glitch) are isolated in frequency, not in time.

    Only unmasked subcarriers participate, so a guard band next to the edge of the active set
    cannot pull the local median around.
    """
    idx = np.flatnonzero(mask)
    if idx.size < 2 * window + 1:
        return amp

    vals = amp[idx].astype(np.float64)
    # Sliding windows over the *compacted* array, so the DC hole in the middle of the band does
    # not create an artificial discontinuity.
    padded = np.pad(vals, window, mode="edge")
    strides = np.lib.stride_tricks.sliding_window_view(padded, 2 * window + 1)
    med = np.median(strides, axis=1)
    mad = np.median(np.abs(strides - med[:, None]), axis=1)
    sigma = 1.4826 * mad

    bad = np.abs(vals - med) > n_sigma * np.maximum(sigma, 1e-6)
    if not bad.any():
        return amp

    out = amp.copy()
    fixed = vals.copy()
    fixed[bad] = med[bad]
    out[idx] = fixed.astype(amp.dtype)
    return out
