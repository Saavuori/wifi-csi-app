"""Choosing which subcarriers to actually listen to.

Averaging all of them is wrong: the good ones get diluted by the bad ones. Ranking by raw
variance is also wrong, and in a way that is easy to miss — the highest-variance subcarriers
tend to be the ones sitting in a deep fade, where the signal is small, the noise is not, and
the variance you are admiring is thermal.

The obvious correction, ranking by mean-over-variance, is worse still. It is maximized by a
subcarrier that never changes: variance goes to zero, the ratio goes to infinity, and you have
built a detector that preferentially selects the carriers least able to detect anything.

So: two separate steps. Gate on magnitude (mean amplitude is a fade indicator, and belongs in
an exclusion filter), then rank by a metric specific to what you are trying to see.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..ring import Window
from .util import band_power, detrend, uniform_resample, welch_psd

# Respiration, per the PulseFi filter parameters: 6-30 breaths/min.
BREATHING_BAND = (0.1, 0.5)
# Cardiac, 48-130 BPM.
HEART_BAND = (0.8, 2.17)

# Subcarriers averaged for the placement tuner's single number.
_TUNER_TOP_K = 3


@dataclass(slots=True)
class SelectionConfig:
    gate_quantile: float = 0.25  # drop the bottom quarter by mean amplitude
    top_k: int = 8


@dataclass(slots=True)
class Selection:
    """Which subcarriers were chosen, and why. The 'why' goes to the UI so the choice can be
    checked against the waterfall by eye rather than trusted."""

    indices: np.ndarray  # int, into the full n_sub axis, best first
    scores: np.ndarray  # float, aligned with indices
    gated_out: np.ndarray  # int, subcarriers rejected by the magnitude gate
    metric: str

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "indices": self.indices.tolist(),
            "scores": [round(float(s), 4) for s in self.scores],
            "gated_out": self.gated_out.tolist(),
        }


def magnitude_gate(
    window: Window, quantile: float
) -> tuple[np.ndarray, np.ndarray]:
    """Split valid subcarriers into (kept, rejected) by mean amplitude.

    Low mean magnitude *is* the deep-fade signature. This is the one place mean amplitude
    belongs — as an exclusion filter, never as a ranking term.
    """
    valid = np.flatnonzero(window.mask)
    if valid.size == 0 or len(window) == 0:
        return valid, np.array([], dtype=int)

    means = np.nanmean(window.amp[:, valid], axis=0)
    finite = np.isfinite(means)
    if not finite.any():
        return valid, np.array([], dtype=int)

    threshold = np.quantile(means[finite], np.clip(quantile, 0.0, 0.95))
    keep = finite & (means >= threshold)
    return valid[keep], valid[~keep]


def rank_for_motion(
    occupied: Window,
    baseline_var: np.ndarray,
    config: SelectionConfig,
) -> Selection:
    """Rank by var_occupied / var_baseline.

    This measures the thing we actually want — how much the subcarrier separates the two states
    — rather than how lively it looks in isolation. `baseline_var` comes from the ambient
    calibration or from an `empty-room` recording, and is indexed over the full n_sub axis.
    """
    kept, rejected = magnitude_gate(occupied, config.gate_quantile)
    if kept.size == 0:
        return Selection(kept, np.array([]), rejected, "var_ratio")

    var = np.nanvar(occupied.amp[:, kept], axis=0)
    base = np.array(baseline_var[kept], dtype=np.float64)

    # A baseline variance of zero or NaN means the calibration never saw that carrier move.
    # That is a claim about too little data, not about an infinitely sensitive subcarrier, so
    # substitute the typical baseline rather than dividing by something near zero — otherwise
    # the least-informative carriers sweep the top of the ranking.
    known = np.isfinite(base) & (base > 0)
    base[~known] = np.median(base[known]) if known.any() else 1.0
    ratio = var / base

    order = np.argsort(ratio)[::-1][: config.top_k]
    return Selection(kept[order], ratio[order], rejected, "var_ratio")


def rank_for_band(
    window: Window,
    config: SelectionConfig,
    band: tuple[float, float] = BREATHING_BAND,
    fs: float = 20.0,
) -> Selection:
    """Rank by in-band power divided by out-of-band power.

    For breathing there is no labelled baseline to compare against, but there is a known
    frequency band. A noise-dominated subcarrier has broadband noise, so it scores badly here
    automatically — the metric does the deep-fade rejection a second time, for free.

    Out-of-band is everything from 0.02 Hz up to the Nyquist limit outside the band, so both
    slow drift and high-frequency junk count against a subcarrier.
    """
    kept, rejected = magnitude_gate(window, config.gate_quantile)
    if kept.size == 0 or len(window) < 16:
        return Selection(kept, np.zeros(kept.size), rejected, "band_snr")

    series, _, _ = uniform_resample(window.t_us, window.amp[:, kept], fs)
    if series.shape[0] < 16:
        return Selection(kept, np.zeros(kept.size), rejected, "band_snr")

    # Segment length must resolve the band, not just cover it. The breathing band is 0.4 Hz
    # wide; Welch's default segmenting would give ~0.2 Hz bins, which puts the whole band in
    # two bins and makes the ranking a coin flip. Ask for at least eight bins across the band.
    nperseg = min(series.shape[0], max(64, int(np.ceil(fs * 8 / max(band[1] - band[0], 1e-3)))))
    freqs, psd = welch_psd(detrend(series), fs, nperseg=nperseg)
    in_band = band_power(freqs, psd, band[0], band[1])
    total = band_power(freqs, psd, 0.02, fs / 2)
    out_band = np.maximum(total - in_band, 1e-12)

    # Power *density* on both sides, not raw power. The out-of-band range is ~10 Hz wide and
    # the breathing band is 0.4 Hz, so comparing the integrals makes a perfectly good
    # respiration peak come out at a ratio below one — a negative dB reading for a signal you
    # can see by eye. Dividing each by its bandwidth is a constant factor across subcarriers,
    # so the ranking is identical, but the number becomes a quantity a human can act on. That
    # matters because this same score is what the placement tuner displays.
    in_width = max(band[1] - band[0], 1e-6)
    out_width = max((fs / 2 - 0.02) - in_width, 1e-6)
    snr = (in_band / in_width) / (out_band / out_width)

    order = np.argsort(snr)[::-1][: config.top_k]
    return Selection(kept[order], snr[order], rejected, "band_snr")


def band_snr_db(
    window: Window,
    band: tuple[float, float],
    fs: float = 20.0,
    config: SelectionConfig | None = None,
) -> float:
    """Single scalar for the placement tuner: peak-to-noise-floor of the best subcarrier, in dB.

    Deliberately not the ranking metric from `rank_for_band`. That one integrates power across
    the whole band, which is right for choosing between subcarriers and wrong for a number a
    human reads while walking around holding a board: a band integral moves only a few dB
    between no respiration at all and a strong signal, because most of the band is noise either
    way. Measured on synthetic scenes, chest displacement from 0 to 9 mm moved it about 4 dB —
    not enough to tell whether the last step helped.

    Peak-in-band over the median out-of-band level asks the question that actually matters:
    is there a *line* here. A respiration signal is narrow, so it lifts the peak without
    lifting the floor, and the number moves tens of dB across the same range.

    The Fresnel-zone literature says the correct way to find a good position is trial and
    error. The job here is to make trial and error fast, which means a number that responds.
    """
    config = config or SelectionConfig()
    kept, _ = magnitude_gate(window, config.gate_quantile)
    if kept.size == 0 or len(window) < 32:
        return -60.0

    series, _, _ = uniform_resample(window.t_us, window.amp[:, kept], fs)
    if series.shape[0] < 32:
        return -60.0

    nperseg = min(series.shape[0], max(64, int(np.ceil(fs * 8 / max(band[1] - band[0], 1e-3)))))
    freqs, psd = welch_psd(detrend(series), fs, nperseg=nperseg)

    in_band = (freqs >= band[0]) & (freqs <= band[1])
    out_band = (freqs >= 0.02) & ~in_band
    if not in_band.any() or not out_band.any():
        return -60.0

    peak = psd[in_band].max(axis=0)
    # Median, not mean: a second peak elsewhere in the spectrum should not be allowed to raise
    # the apparent noise floor and hide a good position.
    floor = np.median(psd[out_band], axis=0)
    ratio = peak / np.maximum(floor, 1e-15)

    # Mean of the best few rather than the single best. One subcarrier's peak wanders by a
    # decibel or two between windows, which shows up as the number twitching while you are
    # standing still trying to decide whether a move helped.
    best = float(np.mean(np.sort(ratio)[::-1][: min(_TUNER_TOP_K, ratio.size)]))
    if not np.isfinite(best) or best <= 0:
        return -60.0
    return float(np.clip(10.0 * np.log10(best), -60.0, 60.0))
