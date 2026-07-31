"""Phase 5 and 6: respiration and heart rate from CSI amplitude.

The chain follows PulseFi, which ran on this hardware class and published the parameters:

    amplitude -> DC removal -> 3rd-order Butterworth bandpass -> Savitzky-Golay -> windowed
    FFT -> peak pick -> BPM

Window length is the dominant parameter, and not subtly: published respiration MAE improves
from 0.61 breaths/min at a 1 s window to 0.09 at 20 s. A 1 s window barely contains one breath,
so there is nothing for a spectral method to lock onto. 15-20 s is the working range for
breathing, and the UI exposes it as a slider because the effect is large enough to see.

Heart rate uses the same code with a different band and a 5 s window, and should be expected to
work only for a near-motionless person at close range. On 64-subcarrier ESP32 CSI the published
result plateaus around 0.50 BPM MAE no matter how long the window gets, against 0.17 on
234-subcarrier hardware with an identical algorithm. That gap is the subcarrier count, not the
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import butter, savgol_filter, sosfiltfilt

from ..ring import History
from .selection import BREATHING_BAND, HEART_BAND, SelectionConfig, rank_for_band
from .util import detrend, uniform_resample

# Everything frequency-domain runs on this grid. 20 Hz is ~9x the top of the cardiac band, so
# it is generous for both, and it keeps a 20 s window at 400 samples — small enough that
# filtfilt and an SVD are free, large enough that the FFT resolves 0.05 Hz before zero padding.
ANALYSIS_FS = 20.0

# Savitzky-Golay parameters from PulseFi. At 20 Hz a 15-sample window is 0.75 s: it removes
# sample-scale jitter while leaving a 2 Hz heartbeat almost untouched.
SG_WINDOW = 15
SG_POLY = 3

# Half-width of the "is this a peak or a hump" test, in Hz. 0.03 Hz is 1.8 breaths/min — wide
# enough to contain a real respiration line and the leakage around it, narrow enough that a
# broad noise hump scores badly.
_PEAK_HALFWIDTH_HZ = 0.03


@dataclass(slots=True)
class VitalsConfig:
    band: tuple[float, float] = BREATHING_BAND
    window_s: float = 20.0
    # How many of the top-ranked carriers to combine. Averaging independent estimates beats
    # trusting the single best one: measured on synthetic 14 BPM scenes at a 20 s window,
    # MAE falls from 0.52 breaths/min with one carrier to 0.24 with eight. Past that the
    # ranking starts admitting carriers with no respiration content and the gain flattens.
    n_subcarriers: int = 8
    zero_pad: int = 8  # FFT length multiplier, for peak interpolation
    selection: SelectionConfig = field(default_factory=SelectionConfig)

    # Coverage limits, checked against what `uniform_resample` had to invent.
    #
    # 0.5 s is the number that matters. Linear interpolation across a hole is a ramp, and a ramp
    # lasting 0.5 s has its energy around 2 Hz and above — outside the respiration band and
    # mostly harmless. Stretch it to a second or two and the fundamental slides down into
    # 0.1-0.5 Hz, on top of the breathing peak, where it is indistinguishable from a breath.
    #
    # 0.2 is deliberately loose by comparison. Many small holes are the normal condition on a
    # shared access point and they average out; one long one does not.
    max_gap_s: float = 0.5
    max_interpolated: float = 0.2

    @classmethod
    def breathing(cls) -> VitalsConfig:
        return cls(band=BREATHING_BAND, window_s=20.0)

    @classmethod
    def heart(cls) -> VitalsConfig:
        # 5 s, because a 1 s window may contain zero heartbeats. Shorter than breathing because
        # the cardiac signal only survives while the subject is motionless anyway, and a long
        # window makes that requirement harder to meet.
        return cls(band=HEART_BAND, window_s=5.0, n_subcarriers=12)


@dataclass(slots=True)
class VitalsResult:
    bpm: float
    confidence: float  # 0..1, peak power as a fraction of in-band power
    snr_db: float  # in-band over out-of-band, on the combined spectrum
    band: tuple[float, float]
    window_s: float
    subcarriers: list[int]
    freqs: np.ndarray  # Hz, in-band only
    spectrum: np.ndarray  # combined magnitude, aligned with freqs
    waveform: np.ndarray  # filtered best subcarrier, normalized to +-1
    waveform_fs: float

    def as_dict(self, waveform_points: int = 400) -> dict:
        step = max(1, int(np.ceil(self.waveform.size / waveform_points)))
        wave = self.waveform[::step]
        return {
            "bpm": round(self.bpm, 2),
            "confidence": round(self.confidence, 3),
            "snr_db": round(self.snr_db, 1),
            "band": list(self.band),
            "window_s": self.window_s,
            "subcarriers": self.subcarriers,
            "freqs": [round(float(f), 4) for f in self.freqs],
            "spectrum": [round(float(v), 5) for v in self.spectrum],
            "waveform": [round(float(v), 4) for v in wave],
            "waveform_fs": round(self.waveform_fs / step, 3),
        }


class VitalsEstimator:
    """One per (node, band). Stateless between calls apart from the config."""

    def __init__(self, config: VitalsConfig | None = None) -> None:
        self.config = config or VitalsConfig.breathing()
        # Why the last estimate declined to produce a number, for the UI to show instead of a
        # stale value. Only the coverage rejection is worth naming: the others are ordinary
        # not-enough-data-yet conditions that resolve on their own within a window.
        self.last_rejection: str | None = None

    def estimate(self, history: History) -> VitalsResult | None:
        cfg = self.config
        self.last_rejection = None
        window = history.seconds(cfg.window_s)
        # Require most of the requested window to actually be present. A 20 s window that is
        # really 6 s of data still produces a confident-looking peak, and it will be wrong.
        if window.duration_s < 0.75 * cfg.window_s or len(window) < 32:
            return None

        ranked = rank_for_band(
            window,
            SelectionConfig(gate_quantile=cfg.selection.gate_quantile, top_k=cfg.n_subcarriers),
            band=cfg.band,
            fs=ANALYSIS_FS,
        )
        if ranked.indices.size == 0:
            return None

        series, fs, coverage = uniform_resample(
            window.t_us, window.amp[:, ranked.indices], ANALYSIS_FS
        )
        # Enough samples for the filter's padding and a little more. Deliberately permissive:
        # short windows give bad answers here, and the UI slider exists so that can be seen
        # rather than hidden behind a refusal to compute.
        if series.shape[0] < 2 * SG_WINDOW:
            return None

        # The one place where being permissive is wrong. A hole in the window is not a shortage
        # of data that makes the answer noisy — the interpolator fills it with a straight line,
        # and a ramp lasting a fraction of a second is *itself* energy in the respiration band.
        # The estimate that comes out is confident and wrong, and nothing downstream can tell.
        # This is the failure mode a node sharing an access point with the household has and a
        # node measuring a dedicated transmitter does not, so it is checked here rather than
        # being left to the rate statistic.
        if not coverage.acceptable(
            max_gap_s=cfg.max_gap_s, max_interpolated=cfg.max_interpolated
        ):
            self.last_rejection = (
                f"link gaps: longest {coverage.max_gap_s:.2f} s, "
                f"{coverage.interpolated * 100:.0f}% interpolated"
            )
            return None

        filtered = bandpass(series, fs, cfg.band)
        if filtered is None:
            return None
        filtered = smooth(filtered)

        freqs, mags = _spectra(filtered, fs, cfg.zero_pad)
        in_band = (freqs >= cfg.band[0]) & (freqs <= cfg.band[1])
        if in_band.sum() < 3:
            return None

        # Per-subcarrier normalization before combining. Without it the loudest carrier decides
        # the answer on its own, which throws away the independent evidence the others carry.
        norm = mags / np.maximum(mags.max(axis=0, keepdims=True), 1e-12)
        combined = norm.mean(axis=1)

        bpm, _ = _peak_bpm(freqs[in_band], combined[in_band])
        confidence = _peak_concentration(freqs[in_band], combined[in_band], bpm / 60.0)
        band_total = float(np.mean(combined[in_band]))
        out_total = float(np.mean(combined[~in_band])) if (~in_band).any() else 1e-12
        snr_db = float(10.0 * np.log10(max(band_total / max(out_total, 1e-12), 1e-12)))

        best = filtered[:, 0]
        scale = np.max(np.abs(best)) or 1.0

        return VitalsResult(
            bpm=bpm,
            confidence=confidence,
            snr_db=snr_db,
            band=cfg.band,
            window_s=cfg.window_s,
            subcarriers=ranked.indices.tolist(),
            freqs=freqs[in_band],
            spectrum=combined[in_band],
            waveform=(best / scale).astype(np.float32),
            waveform_fs=fs,
        )


def bandpass(x: np.ndarray, fs: float, band: tuple[float, float]) -> np.ndarray | None:
    """3rd-order Butterworth bandpass, zero-phase.

    Second-order-sections rather than transfer-function coefficients: at these normalized
    frequencies (0.1 Hz against a 20 Hz sample rate is 0.01 of Nyquist) a `ba` implementation
    is numerically hopeless, and will quietly return noise instead of failing.

    Zero-phase because the waveform is displayed next to the spectrum and a phase-distorted
    trace next to a correct BPM invites exactly the wrong conclusion about which to trust.
    """
    lo, hi = band
    nyq = fs / 2.0
    if not (0 < lo < hi < nyq):
        return None

    sos = butter(3, [lo / nyq, hi / nyq], btype="bandpass", output="sos")
    # filtfilt needs padding room; sosfiltfilt's default padlen is 3 * (2 * n_sections + 1).
    if x.shape[0] <= 3 * (2 * sos.shape[0] + 1):
        return None
    # Step 2 of the pipeline: remove DC (and the linear drift that a pure mean-subtraction
    # leaves behind) before filtering, so the filter's transient is not chasing an offset.
    return sosfiltfilt(sos, detrend(x, axis=0), axis=0)


def smooth(x: np.ndarray) -> np.ndarray:
    """Savitzky-Golay, window 15, order 3 — the PulseFi parameters."""
    if x.shape[0] <= SG_WINDOW:
        return x
    return savgol_filter(x, SG_WINDOW, SG_POLY, axis=0)


def _spectra(x: np.ndarray, fs: float, zero_pad: int) -> tuple[np.ndarray, np.ndarray]:
    """Hann-windowed magnitude spectra of each column, zero-padded.

    Zero padding does not add information, but it does interpolate the existing spectrum onto a
    finer grid, which is what lets the peak be located to better than one bin without the
    parabolic fit having to do all the work.
    """
    n = x.shape[0]
    win = np.hanning(n)[:, None]
    nfft = int(2 ** np.ceil(np.log2(n * max(1, zero_pad))))
    spec = np.fft.rfft(x * win, n=nfft, axis=0)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / fs)
    return freqs, np.abs(spec)


def _peak_concentration(freqs: np.ndarray, mag: np.ndarray, f_peak: float) -> float:
    """Fraction of in-band energy sitting within `_PEAK_HALFWIDTH_HZ` of the peak.

    Deliberately not "peak bin over total", which shrinks as the zero-padding factor grows and
    would make the confidence number a property of an FFT setting rather than of the signal. A
    respiration peak is a narrow line on top of a noise floor; this measures how much of the
    band's energy is actually in that line.
    """
    if mag.size == 0:
        return 0.0
    near = np.abs(freqs - f_peak) <= _PEAK_HALFWIDTH_HZ
    total = float(np.sum(mag**2))
    if total <= 0:
        return 0.0
    return float(np.clip(np.sum(mag[near] ** 2) / total, 0.0, 1.0))


def _peak_bpm(freqs: np.ndarray, mag: np.ndarray) -> tuple[float, float]:
    """Locate the in-band peak and convert to BPM. Returns (bpm, peak magnitude)."""
    i = int(np.argmax(mag))
    f = float(freqs[i])

    # Parabolic interpolation on the log magnitude around the peak. Cheap, and it removes the
    # bias you get from always reporting a bin centre.
    if 0 < i < mag.size - 1:
        y0, y1, y2 = np.log(np.maximum(mag[i - 1 : i + 2], 1e-12))
        denom = y0 - 2 * y1 + y2
        if denom != 0:
            delta = 0.5 * (y0 - y2) / denom
            if abs(delta) <= 1.0:
                f += float(delta) * float(freqs[1] - freqs[0])

    return f * 60.0, float(mag[i])
