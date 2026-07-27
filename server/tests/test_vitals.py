"""Breathing and heart rate against synthetic scenes with a known correct answer.

Tolerances here are loose relative to what the published work reports, on purpose: they are
regression guards, not a claim about accuracy. The one thing they assert sharply is the shape
of the window-length relationship, because that is the parameter the plan says dominates
everything else and the one a user will actually be adjusting.
"""

from __future__ import annotations

import numpy as np
import pytest

from csi.dsp.vitals import VitalsConfig, VitalsEstimator, bandpass


def estimate(ring, window_s=20.0, **kwargs):
    config = VitalsConfig.breathing()
    config.window_s = window_s
    for key, value in kwargs.items():
        setattr(config, key, value)
    return VitalsEstimator(config).estimate(ring)


@pytest.mark.parametrize("true_bpm", [8.0, 14.0, 22.0])
def test_recovers_the_breathing_rate(build_ring, true_bpm):
    ring, _ = build_ring(45.0, breathing_bpm=true_bpm)
    result = estimate(ring)

    assert result is not None
    assert result.bpm == pytest.approx(true_bpm, abs=1.5)


def test_longer_windows_are_better(build_ring):
    """The plan's central claim about this phase: MAE falls steeply with window length, and a
    1-2 s window barely contains one breath. If this inverts, the pipeline is broken even when
    a single estimate happens to look right."""
    errors = {}
    for window_s in (2.0, 20.0):
        seeds = []
        for seed in range(3):
            ring, _ = build_ring(45.0, breathing_bpm=14.0, seed=seed)
            result = estimate(ring, window_s=window_s)
            if result is not None:
                seeds.append(abs(result.bpm - 14.0))
        errors[window_s] = float(np.mean(seeds))

    assert errors[20.0] < errors[2.0] / 3, errors


def test_reports_which_subcarriers_it_used(build_ring):
    """The answer is only checkable if you can see what it was computed from."""
    ring, _ = build_ring(45.0, breathing_bpm=14.0)
    result = estimate(ring)

    assert len(result.subcarriers) == VitalsConfig.breathing().n_subcarriers
    assert len(set(result.subcarriers)) == len(result.subcarriers)
    assert all(0 <= i < 64 for i in result.subcarriers)


def test_returns_none_when_the_window_is_not_actually_full(build_ring):
    """A 20 s window that holds 3 s of data still yields a confident-looking peak, and it will
    be wrong. Refusing is the correct output."""
    ring, _ = build_ring(3.0, breathing_bpm=14.0)
    assert estimate(ring, window_s=20.0) is None


def test_spectrum_and_waveform_are_shaped_for_the_ui(build_ring):
    ring, _ = build_ring(45.0, breathing_bpm=14.0)
    result = estimate(ring)

    assert result.freqs.shape == result.spectrum.shape
    assert result.freqs.min() >= result.band[0] - 1e-9
    assert result.freqs.max() <= result.band[1] + 1e-9
    assert np.abs(result.waveform).max() == pytest.approx(1.0, abs=1e-5)

    payload = result.as_dict(waveform_points=100)
    assert len(payload["waveform"]) <= 100
    assert payload["waveform_fs"] < result.waveform_fs


def test_confidence_is_independent_of_the_zero_padding_factor(build_ring):
    """Confidence must describe the signal, not an FFT setting. A peak-bin-over-total measure
    would shrink as padding grows, which makes it useless as a threshold."""
    ring, _ = build_ring(45.0, breathing_bpm=14.0)

    low = estimate(ring, zero_pad=2)
    high = estimate(ring, zero_pad=16)
    assert low.confidence == pytest.approx(high.confidence, abs=0.12)


def test_survives_dropped_frames(build_ring):
    """UDP loses frames and the device drops them when its ring is full. The resampler exists
    so that this costs accuracy rather than correctness."""
    ring, _ = build_ring(45.0, breathing_bpm=14.0, drop_rate=0.05)
    result = estimate(ring)

    assert result is not None
    assert result.bpm == pytest.approx(14.0, abs=2.0)


def test_agc_steps_do_not_move_the_answer(build_ring):
    """Gain steps every two seconds must not shift the reported rate. This is the whole reason
    the AGC work happens before Phase 4."""
    calm, _ = build_ring(45.0, breathing_bpm=14.0, agc_period_s=1000.0)
    stepping, _ = build_ring(45.0, breathing_bpm=14.0, agc_period_s=2.0)

    assert estimate(stepping).bpm == pytest.approx(estimate(calm).bpm, abs=1.5)


def test_heart_rate_needs_its_own_band(build_ring):
    """A cardiac rate sits far outside the respiration band; running the breathing config over
    a heartbeat must not report the heart rate by accident."""
    ring, _ = build_ring(30.0, breathing_bpm=14.0, heart_bpm=66.0)

    breathing = estimate(ring)
    assert breathing.bpm < 30.0, "the breathing band cannot contain 66 BPM"

    heart = VitalsEstimator(VitalsConfig.heart()).estimate(ring)
    assert heart is not None
    assert 48.0 <= heart.bpm <= 130.0, "must at least stay inside its own band"


# -- filter -----------------------------------------------------------------------------


def test_bandpass_rejects_out_of_band_tones():
    fs = 20.0
    t = np.arange(int(60 * fs)) / fs
    signal = np.sin(2 * np.pi * 0.25 * t) + 3.0 * np.sin(2 * np.pi * 3.0 * t) + 5.0

    out = bandpass(signal[:, None], fs, (0.1, 0.5))
    assert out is not None

    spectrum = np.abs(np.fft.rfft(out[:, 0] * np.hanning(out.shape[0])))
    freqs = np.fft.rfftfreq(out.shape[0], 1 / fs)
    assert freqs[np.argmax(spectrum)] == pytest.approx(0.25, abs=0.02)
    assert np.max(spectrum[freqs > 1.0]) < 0.01 * np.max(spectrum)
    # The input carries a DC offset of 5.0; what survives is filtfilt's edge transient, not
    # leakage, so this checks the attenuation rather than demanding an exact zero.
    assert abs(out[:, 0].mean()) < 5.0 / 100


def test_bandpass_refuses_an_impossible_band():
    """Above Nyquist there is no filter to design. Returning None beats returning noise."""
    assert bandpass(np.zeros((400, 1)), 20.0, (0.1, 15.0)) is None
    assert bandpass(np.zeros((400, 1)), 20.0, (0.5, 0.1)) is None


def test_bandpass_refuses_a_window_too_short_to_pad():
    assert bandpass(np.zeros((10, 1)), 20.0, (0.1, 0.5)) is None
