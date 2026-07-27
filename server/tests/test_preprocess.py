"""Preprocessing: subcarrier masking, AGC removal, impulse rejection.

The AGC tests are the important ones. An AGC step is indistinguishable from major motion to a
raw variance detector, it happens every few seconds, and if it is not handled the presence
detector produces constant false positives — so these assert on the *discrimination*, not just
that the code runs.
"""

from __future__ import annotations

import numpy as np
import pytest

from csi.dsp.preprocess import PreprocessConfig, Preprocessor, hampel
from csi.dsp.subcarriers import index_to_k, valid_mask
from csi.protocol import Frame


def frame_from_amplitude(amp: np.ndarray, *, rssi: int = -50, seq: int = 1, t_us: int = 0) -> Frame:
    """Build a Frame whose per-subcarrier amplitude is `amp` (phase is arbitrary but fixed)."""
    n = amp.size
    phase = np.linspace(0, 3.0, n)
    re = np.clip(np.rint(amp * np.cos(phase)), -127, 127)
    im = np.clip(np.rint(amp * np.sin(phase)), -127, 127)
    data = np.empty(2 * n, dtype=np.int8)
    data[0::2] = im
    data[1::2] = re
    return Frame(
        node_id=1,
        seq=seq,
        timestamp=t_us,
        rssi=rssi,
        noise_floor=-92,
        channel=6,
        sec_channel=0,
        n_sub=n,
        data=data,
    )


# -- subcarrier layout ------------------------------------------------------------------


def test_ht20_mask_drops_dc_guards_and_pilots():
    mask = valid_mask(64)
    k = index_to_k(64)

    assert not mask[0], "DC is not transmitted"
    for pilot in (-21, -7, 7, 21):
        assert not mask[np.flatnonzero(k == pilot)[0]], f"pilot {pilot} should be excluded"
    assert not mask[np.flatnonzero(k == 30)[0]], "guard band"
    assert mask[np.flatnonzero(k == 10)[0]], "subcarrier 10 carries data"
    assert mask.sum() == 48, "52 active minus 4 pilots"


def test_pilots_can_be_kept():
    assert valid_mask(64, drop_pilots=False).sum() == 52


@pytest.mark.parametrize("n_sub", [64, 128, 256])
def test_every_supported_bandwidth_has_a_sane_mask(n_sub):
    mask = valid_mask(n_sub)
    assert 0 < mask.sum() < n_sub
    assert not mask[0], "DC is never valid"


def test_unknown_bandwidth_falls_back_conservatively():
    """An unrecognized subcarrier count must not throw and must not claim everything is valid."""
    mask = valid_mask(100)
    assert not mask[0]
    assert 0 < mask.sum() < 100


def test_mask_is_read_only():
    """It is cached and shared across every frame at that bandwidth; a caller mutating it in
    place would silently corrupt every other node."""
    with pytest.raises(ValueError):
        valid_mask(64)[5] = False


# -- AGC --------------------------------------------------------------------------------


def test_uniform_gain_step_is_flagged():
    """Every subcarrier jumping by the same factor is the receiver, not the room."""
    pre = Preprocessor()
    base = np.full(64, 40.0)

    pre.process(frame_from_amplitude(base, t_us=0))
    pre.process(frame_from_amplitude(base, t_us=12500))
    stepped = pre.process(frame_from_amplitude(base * 2.0, t_us=25000))

    assert stepped.agc_step


def test_frequency_selective_change_is_not_flagged_as_agc():
    """Real motion hits subcarriers unevenly. A detector that flags this would throw away the
    signal it exists to find."""
    pre = Preprocessor()
    base = np.full(64, 40.0)
    rng = np.random.default_rng(0)
    selective = base * (1.0 + rng.uniform(-0.6, 0.6, 64))

    pre.process(frame_from_amplitude(base, t_us=0))
    pre.process(frame_from_amplitude(base, t_us=12500))
    changed = pre.process(frame_from_amplitude(selective, t_us=25000))

    assert not changed.agc_step


def test_small_uniform_drift_is_not_flagged():
    """Below the step threshold it is noise, and flagging it would exclude half the frames from
    variance accumulation for no reason."""
    pre = Preprocessor()
    base = np.full(64, 40.0)

    pre.process(frame_from_amplitude(base, t_us=0))
    pre.process(frame_from_amplitude(base, t_us=12500))
    drifted = pre.process(frame_from_amplitude(base * 1.02, t_us=25000))

    assert not drifted.agc_step


def test_normalization_removes_a_gain_step():
    """The primary defense: after normalization the amplitudes either side of a 6 dB AGC step
    must be close, or the variance detector sees a person who is not there."""
    pre = Preprocessor(PreprocessConfig(norm_mode="rms"))
    base = np.full(64, 40.0)

    before = pre.process(frame_from_amplitude(base, t_us=0))
    after = pre.process(frame_from_amplitude(base * 2.0, t_us=12500))

    mask = before.mask
    ratio = after.amp[mask] / before.amp[mask]
    assert np.allclose(ratio, 1.0, atol=0.05), f"gain step survived: {ratio.min()}..{ratio.max()}"


def test_normalization_preserves_relative_shape():
    """Removing gain must not remove the frequency-selective structure that carries the signal."""
    pre = Preprocessor(PreprocessConfig(norm_mode="rms", hampel_enabled=False))
    rng = np.random.default_rng(3)
    shape = 40.0 * (1.0 + rng.uniform(-0.4, 0.4, 64))

    processed = pre.process(frame_from_amplitude(shape, t_us=0))
    mask = processed.mask

    raw = frame_from_amplitude(shape).amplitude()[mask]
    got = processed.amp[mask]
    # Same up to a single scale factor.
    assert np.corrcoef(raw, got)[0, 1] > 0.999


def test_reset_prevents_a_false_step_across_a_discontinuity():
    """A node reboot or a replay seek is not a gain event, and must not be reported as one."""
    pre = Preprocessor()
    pre.process(frame_from_amplitude(np.full(64, 40.0), t_us=0))
    pre.process(frame_from_amplitude(np.full(64, 40.0), t_us=12500))

    pre.reset()
    after = pre.process(frame_from_amplitude(np.full(64, 120.0), t_us=0))
    assert not after.agc_step


def test_bandwidth_change_resets_state_instead_of_crashing():
    pre = Preprocessor()
    pre.process(frame_from_amplitude(np.full(64, 40.0)))
    wide = pre.process(frame_from_amplitude(np.full(128, 40.0)))
    assert wide.amp.size == 128


# -- Hampel -----------------------------------------------------------------------------


def test_hampel_replaces_an_isolated_spike():
    amp = np.full(64, 40.0)
    mask = valid_mask(64)
    spike_at = np.flatnonzero(mask)[10]
    amp[spike_at] = 400.0

    cleaned = hampel(amp, mask, window=5, n_sigma=3.0)
    assert cleaned[spike_at] == pytest.approx(40.0, abs=1.0)


def test_hampel_leaves_a_smooth_channel_alone():
    """A real channel has structure. The filter must not flatten it into its own median."""
    mask = valid_mask(64)
    amp = 40.0 + 12.0 * np.sin(np.linspace(0, 2 * np.pi, 64))

    cleaned = hampel(amp.copy(), mask, window=5, n_sigma=3.0)
    np.testing.assert_allclose(cleaned[mask], amp[mask], rtol=1e-6)


def test_hampel_ignores_masked_subcarriers():
    """Guard bands hold arbitrary values; letting them into the local median would drag the
    edges of the active band around."""
    mask = valid_mask(64)
    amp = np.full(64, 40.0)
    amp[~mask] = 5000.0

    cleaned = hampel(amp, mask, window=5, n_sigma=3.0)
    np.testing.assert_allclose(cleaned[mask], 40.0)


# -- masking end to end -----------------------------------------------------------------


def test_masked_subcarriers_come_out_as_nan():
    processed = Preprocessor().process(frame_from_amplitude(np.full(64, 40.0)))
    assert np.isnan(processed.amp[~processed.mask]).all()
    assert np.isfinite(processed.amp[processed.mask]).all()


def test_norm_mode_none_leaves_amplitude_untouched():
    frame = frame_from_amplitude(np.full(64, 40.0))
    config = PreprocessConfig(norm_mode="none", hampel_enabled=False)
    processed = Preprocessor(config).process(frame)
    np.testing.assert_allclose(processed.amp[processed.mask], frame.amplitude()[processed.mask])
