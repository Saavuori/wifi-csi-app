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

    series, _ = uniform_resample(window.t_us, window.amp[:, kept], fs)
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
    snr = in_band / out_band

    order = np.argsort(snr)[::-1][: config.top_k]
    return Selection(kept[order], snr[order], rejected, "band_snr")


def band_snr_db(window: Window, band: tuple[float, float], fs: float = 20.0) -> float:
    """Single scalar: the best in-band SNR available across all valid subcarriers, in dB.

    This is what the placement tuner shows. One number, updated continuously, that gets bigger
    when the node is in a better spot — the Fresnel-zone literature says the correct method for
    finding that spot is trial and error, so the job here is to make trial and error fast.
    """
    sel = rank_for_band(window, SelectionConfig(top_k=1), band=band, fs=fs)
    if sel.scores.size == 0 or not np.isfinite(sel.scores[0]) or sel.scores[0] <= 0:
        return -60.0
    return float(np.clip(10.0 * np.log10(sel.scores[0]), -60.0, 60.0))
