"""Subcarrier gating and ranking.

The two negative results the plan warns about are asserted directly, because both are things a
reasonable person would implement by accident:

  * ranking by raw variance promotes deep-fade carriers, which are noise-dominated
  * ranking by mean-over-variance promotes carriers that never change at all
"""

from __future__ import annotations

import numpy as np
import pytest

from csi.dsp.selection import (
    BREATHING_BAND,
    SelectionConfig,
    band_snr_db,
    magnitude_gate,
    rank_for_band,
    rank_for_motion,
)
from csi.dsp.subcarriers import valid_mask
from csi.ring import Window


def synthetic_window(n=800, fs=20.0, *, seed=0) -> Window:
    """A window with three deliberately different kinds of subcarrier.

    Indices are chosen among the valid HT20 set:
      - `strong`   large mean, a clean 0.25 Hz respiration line
      - `faded`    tiny mean, large *relative* noise — the trap for a raw-variance ranking
      - `static`   large mean, almost no variation — the trap for a mean/variance ranking
    """
    rng = np.random.default_rng(seed)
    mask = valid_mask(64)
    valid = np.flatnonzero(mask)

    t = np.arange(n) / fs
    amp = np.full((n, 64), np.nan, dtype=np.float32)
    for i in valid:
        amp[:, i] = 40.0 + rng.normal(0, 0.4, n)

    strong, faded, static = int(valid[5]), int(valid[15]), int(valid[25])
    amp[:, strong] = 40.0 + 1.2 * np.sin(2 * np.pi * 0.25 * t) + rng.normal(0, 0.15, n)
    amp[:, faded] = 2.0 + rng.normal(0, 3.0, n)
    amp[:, static] = 40.0 + rng.normal(0, 0.01, n)

    return Window(
        t_us=(t * 1e6).astype(np.int64),
        amp=amp,
        agc=np.zeros(n, dtype=bool),
        rssi=np.full(n, -50, dtype=np.int8),
        mask=mask,
    )


# -- the gate ----------------------------------------------------------------------------


def test_gate_drops_the_deep_fade_carrier():
    """Low mean magnitude is the deep-fade signature, and this is where mean amplitude belongs
    — as an exclusion filter, not as a ranking term."""
    window = synthetic_window()
    faded = int(np.flatnonzero(window.mask)[15])

    kept, rejected = magnitude_gate(window, 0.25)
    assert faded in rejected
    assert faded not in kept


def test_gate_quantile_controls_how_much_is_dropped():
    window = synthetic_window()
    n_valid = int(window.mask.sum())

    for quantile in (0.0, 0.25, 0.5):
        kept, rejected = magnitude_gate(window, quantile)
        assert kept.size + rejected.size == n_valid
        assert kept.size == pytest.approx(n_valid * (1 - quantile), abs=3)


def test_gate_never_returns_masked_subcarriers():
    window = synthetic_window()
    kept, rejected = magnitude_gate(window, 0.25)
    assert window.mask[kept].all()
    assert window.mask[rejected].all()


# -- ranking for breathing ----------------------------------------------------------------


def test_band_ranking_prefers_the_carrier_with_the_respiration_line():
    window = synthetic_window()
    strong = int(np.flatnonzero(window.mask)[5])

    ranked = rank_for_band(window, SelectionConfig(top_k=3), band=BREATHING_BAND)
    assert strong in ranked.indices.tolist()
    assert ranked.indices[0] == strong


def test_band_ranking_rejects_the_noise_dominated_carrier():
    """A deep-fade carrier's noise is broadband, so an in-band-over-out-of-band metric rejects
    it on its own — the gate and the ranking catch it independently."""
    window = synthetic_window()
    faded = int(np.flatnonzero(window.mask)[15])

    ranked = rank_for_band(window, SelectionConfig(gate_quantile=0.0, top_k=8))
    assert faded not in ranked.indices.tolist()


def test_band_ranking_does_not_promote_the_static_carrier():
    """The mean-over-variance trap: a carrier that never changes maximizes that ratio, and it
    is exactly the carrier least able to detect anything."""
    window = synthetic_window()
    static = int(np.flatnonzero(window.mask)[25])

    ranked = rank_for_band(window, SelectionConfig(top_k=5))
    assert static not in ranked.indices.tolist()


def test_ranking_is_ordered_best_first():
    window = synthetic_window()
    ranked = rank_for_band(window, SelectionConfig(top_k=6))
    assert list(ranked.scores) == sorted(ranked.scores, reverse=True)


def test_selection_serializes_for_the_ui():
    """The UI overlays the chosen subcarriers on the waterfall so the choice can be checked by
    eye; that needs the rejects too, not just the picks."""
    window = synthetic_window()
    payload = rank_for_band(window, SelectionConfig(top_k=4)).as_dict()

    assert payload["metric"] == "band_snr"
    assert len(payload["indices"]) == 4
    assert len(payload["scores"]) == 4
    assert payload["gated_out"]


# -- ranking for motion --------------------------------------------------------------------


def test_motion_ranking_uses_the_baseline_not_raw_variance():
    """Raw variance would pick the fade carrier, whose variance is largest and whose signal is
    nothing. Ranking against the empty-room baseline picks the one that actually changed."""
    window = synthetic_window()
    valid = np.flatnonzero(window.mask)
    strong, faded = int(valid[5]), int(valid[15])

    baseline = np.full(64, np.nan)
    baseline[valid] = 0.16  # everything was quiet when the room was empty...
    baseline[faded] = 9.0  # ...except the fade carrier, which was already noisy

    ranked = rank_for_motion(window, baseline, SelectionConfig(gate_quantile=0.0, top_k=3))

    raw_variance_pick = int(valid[np.nanargmax(np.nanvar(window.amp[:, valid], axis=0))])
    assert raw_variance_pick == faded, "the fixture must actually contain the trap"
    assert ranked.indices[0] == strong
    assert faded not in ranked.indices.tolist()


def test_motion_ranking_survives_a_zero_baseline():
    """A baseline variance of zero is a claim about too little data, not an infinitely
    sensitive subcarrier, and must not sweep the top of the ranking."""
    window = synthetic_window()
    valid = np.flatnonzero(window.mask)
    baseline = np.full(64, np.nan)
    baseline[valid] = 0.16
    baseline[valid[9]] = 0.0

    ranked = rank_for_motion(window, baseline, SelectionConfig(top_k=3))
    assert np.isfinite(ranked.scores).all()
    assert ranked.indices[0] == int(valid[5])


# -- the placement tuner ---------------------------------------------------------------------


def test_placement_snr_responds_to_signal_quality():
    """The tuner's one number has to move when the node moves, or it cannot be tuned against."""
    good = synthetic_window()
    quiet = synthetic_window(seed=1)
    valid = np.flatnonzero(quiet.mask)
    quiet.amp[:, valid[5]] = 40.0 + np.random.default_rng(2).normal(0, 0.4, len(quiet))

    assert band_snr_db(good, BREATHING_BAND) > band_snr_db(quiet, BREATHING_BAND) + 3


def test_placement_snr_is_bounded_on_empty_input():
    empty = Window(
        t_us=np.zeros(0, dtype=np.int64),
        amp=np.zeros((0, 64), dtype=np.float32),
        agc=np.zeros(0, dtype=bool),
        rssi=np.zeros(0, dtype=np.int8),
        mask=valid_mask(64),
    )
    assert band_snr_db(empty, BREATHING_BAND) == -60.0


# -- incomplete subcarriers ------------------------------------------------------------------


def holed_window(seed=0):
    """A window where one bright subcarrier is missing two seconds in the middle."""
    window = synthetic_window(seed=seed)
    holed = int(np.flatnonzero(window.mask)[10])
    window.amp[:, holed] = 60.0  # comfortably past the magnitude gate
    window.amp[100:140, holed] = np.nan
    return window, holed


def test_gate_rejects_a_subcarrier_with_a_hole_in_it():
    """The gate used to screen on `isfinite(nanmean(column))`, and `nanmean` ignores exactly the
    NaN being asked about — so a holed column had a finite mean and was admitted."""
    window, holed = holed_window()
    kept, rejected = magnitude_gate(window, 0.25)

    assert holed in rejected
    assert holed not in kept


def test_a_holed_subcarrier_does_not_take_the_whole_analysis_down():
    """`welch(detrend="linear")` runs each segment through `asarray_chkfinite` and raises rather
    than returning NaN. There is no per-carrier guard above this, so admitting one unusable
    column aborts the entire metrics pass — every analysis, for every node."""
    window, holed = holed_window()

    ranked = rank_for_band(window, SelectionConfig(top_k=8), band=BREATHING_BAND)
    assert holed not in ranked.indices.tolist()
    assert ranked.indices.size > 0, "the carriers that are fine must still be ranked"

    snr = band_snr_db(window, BREATHING_BAND)
    assert np.isfinite(snr)
    assert snr == pytest.approx(band_snr_db(synthetic_window(), BREATHING_BAND), abs=1.0)


def test_motion_ranking_skips_a_holed_subcarrier():
    window, holed = holed_window()
    ranked = rank_for_motion(window, np.full(64, 0.16), SelectionConfig(top_k=8))

    assert holed not in ranked.indices.tolist()
    assert np.isfinite(ranked.scores).all()


def test_a_window_with_nothing_usable_selects_nothing():
    """Returning every valid subcarrier as "kept" when none of them is usable hands the
    unusable ones straight downstream, which is the failure this gate exists to prevent."""
    window = synthetic_window()
    window.amp[:] = np.nan

    kept, rejected = magnitude_gate(window, 0.25)
    assert kept.size == 0
    assert rejected.size == int(window.mask.sum())
    assert band_snr_db(window, BREATHING_BAND) == -60.0
    assert rank_for_band(window, SelectionConfig(), band=BREATHING_BAND).indices.size == 0
