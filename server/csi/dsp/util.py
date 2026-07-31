"""Shared numerics for the analysis stages."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class Coverage:
    """How much of a resampled window is measurement and how much is invention.

    `np.interp` cannot fail. Hand it a window with a half-second hole in it and it draws a
    straight line across, returns an array of exactly the expected length, and says nothing —
    and a linear ramp across 400 ms puts a large amount of power right in the 0.1-0.5 Hz
    respiration band. The estimator downstream then reports a confident, wrong breathing rate,
    which is worse than reporting nothing.

    This mattered much less when the frames came from a transmitter board we owned: the holes
    were a millisecond or two of jitter. A node sharing an access point with the rest of the
    house gets holes measured in hundreds of milliseconds whenever the channel is busy, so the
    size of the invention has to travel with the data.
    """

    max_gap_s: float  # longest interval between consecutive real samples
    interpolated: float  # fraction of grid points not bracketed by samples one period apart

    def acceptable(self, *, max_gap_s: float, max_interpolated: float) -> bool:
        return self.max_gap_s <= max_gap_s and self.interpolated <= max_interpolated


def uniform_resample(
    t_us: np.ndarray, values: np.ndarray, fs: float
) -> tuple[np.ndarray, float, Coverage]:
    """Resample irregularly-sampled columns onto a uniform grid at `fs` Hz.

    CSI does not arrive on a metronome. Even a well-behaved node jitters by a millisecond or
    two, and any dropped frame leaves a hole. Feeding that straight into an FFT smears the
    spectrum, and smearing is exactly what ruins a 0.2 Hz breathing peak. Every frequency-domain
    stage resamples first.

    `values` is (n_samples, n_series). Linear interpolation is enough: we are upsampling a
    signal whose content is below 2 Hz onto a grid near 80 Hz, so the interpolation error sits
    far above anything we care about.

    Returns (resampled, actual_fs, coverage). Callers doing anything frequency-domain must check
    the coverage before believing the result — see `Coverage`.
    """
    empty = Coverage(max_gap_s=0.0, interpolated=1.0)
    if t_us.size < 2:
        return np.zeros((0, values.shape[1] if values.ndim > 1 else 0), dtype=np.float32), fs, empty

    t = (t_us - t_us[0]).astype(np.float64) / 1e6
    n = int(np.floor(t[-1] * fs)) + 1
    if n < 2:
        return np.zeros((0, values.shape[1]), dtype=np.float32), fs, empty

    grid = np.arange(n, dtype=np.float64) / fs
    out = np.empty((n, values.shape[1]), dtype=np.float32)
    for j in range(values.shape[1]):
        out[:, j] = np.interp(grid, t, values[:, j].astype(np.float64))

    intervals = np.diff(t)
    # A grid point is "invented" when the two real samples bracketing it are further apart than
    # a couple of sample periods — i.e. the interpolator had nothing nearby to work from.
    # Ordinary jitter stays under the threshold and does not count against the window.
    tolerance = 2.5 / fs
    wide = np.flatnonzero(intervals > tolerance)
    invented = float(np.sum(intervals[wide])) * fs if wide.size else 0.0
    coverage = Coverage(
        max_gap_s=float(intervals.max()) if intervals.size else 0.0,
        interpolated=min(1.0, invented / n),
    )
    return out, fs, coverage


def nan_columns(amp: np.ndarray) -> np.ndarray:
    """Boolean mask of columns containing any NaN, shape (n_sub,)."""
    return np.isnan(amp).any(axis=0)


def robust_std(x: np.ndarray, axis: int | None = None) -> np.ndarray:
    """Median-absolute-deviation standard deviation estimate.

    Used wherever a single outlying frame would otherwise dominate — which, with AGC steps and
    the occasional glitched packet, is most places.
    """
    med = np.median(x, axis=axis, keepdims=True)
    mad = np.median(np.abs(x - med), axis=axis)
    return 1.4826 * mad


def detrend(x: np.ndarray, axis: int = 0) -> np.ndarray:
    """Remove the linear trend along `axis`.

    Removing the DC component alone is not enough over a 20 s window: the channel drifts, and a
    residual ramp puts a large amount of power at the low end of the spectrum, right where the
    breathing peak is. Slope removal costs nothing and moves that power out of the band.
    """
    n = x.shape[axis]
    if n < 3:
        return x - np.mean(x, axis=axis, keepdims=True)
    t = np.arange(n, dtype=np.float64)
    t = (t - t.mean()) / (t.std() or 1.0)
    shape = [1] * x.ndim
    shape[axis] = n
    tt = t.reshape(shape)
    slope = np.sum(x * tt, axis=axis, keepdims=True) / n
    return x - slope * tt - np.mean(x, axis=axis, keepdims=True)


def band_power(
    freqs: np.ndarray, psd: np.ndarray, lo: float, hi: float, axis: int = 0
) -> np.ndarray:
    """Integrate a PSD over [lo, hi] Hz."""
    sel = (freqs >= lo) & (freqs <= hi)
    if not sel.any():
        return np.zeros(psd.shape[1:] if psd.ndim > 1 else ())
    return np.trapezoid(np.take(psd, np.flatnonzero(sel), axis=axis), freqs[sel], axis=axis)


def welch_psd(
    x: np.ndarray, fs: float, nperseg: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Welch PSD along axis 0. Returns (freqs, psd) with psd shaped (n_freq, n_series).

    Welch rather than a single periodogram because we are estimating *where the power is*, not
    resolving two nearby tones — averaging segments trades resolution we do not need for
    variance reduction we do.
    """
    from scipy.signal import welch

    n = x.shape[0]
    if nperseg is None:
        nperseg = min(n, max(64, n // 4))
    nperseg = max(8, min(nperseg, n))
    freqs, psd = welch(x, fs=fs, nperseg=nperseg, axis=0, detrend="linear")
    if psd.ndim == 1:
        psd = psd[:, None]
    return freqs, psd
