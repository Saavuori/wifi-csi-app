"""Phase 4's exit criterion, as a test.

"Correctly flags presence/absence across the empty-room and walking recordings with no manual
retuning between them." That is what `test_flags_presence_without_retuning` asserts: one
detector, one calibration, both scenes.
"""

from __future__ import annotations

import numpy as np
import pytest

from csi.dsp.preprocess import Preprocessor
from csi.dsp.presence import PresenceConfig, PresenceDetector, State
from csi.ring import FrameRing
from csi.synth import Scene, SceneConfig

RATE = 80.0
UPDATE_EVERY = 16  # frames between detector updates, ~5 Hz


def run_scenario(motion_at, duration_s: float, *, config=None, seed=1):
    """Drive a scene through preprocessing into the detector, sampling its output over time.

    Returns a list of (t_seconds, PresenceResult).
    """
    scene = Scene(SceneConfig(rate_hz=RATE, drop_rate=0.002, seed=seed))
    pre = Preprocessor()
    ring = FrameRing(64, int(duration_s * RATE) + 128)
    detector = PresenceDetector(config or PresenceConfig())

    history = []
    for i in range(int(duration_s * RATE)):
        t = i / RATE
        scene.set_motion(motion_at(t))
        frame = scene.next_frame()
        if frame is None:
            continue
        ring.push(pre.process(frame))
        if i % UPDATE_EVERY == 0:
            result = detector.update(ring)
            if result is not None:
                history.append((t, result))
    return history, detector


def states_between(history, lo, hi):
    return {r.state for t, r in history if lo <= t < hi}


def test_calibration_completes_and_settles_to_absent():
    history, detector = run_scenario(lambda t: 0.0, 45.0)

    assert history[0][1].state is State.CALIBRATING
    assert detector.state is State.ABSENT
    assert detector.baseline_var is not None
    assert states_between(history, 35, 45) == {State.ABSENT}


def test_calibration_progress_is_reported_monotonically():
    history, _ = run_scenario(lambda t: 0.0, 45.0)
    progress = [r.calibration_progress for _, r in history if r.state is State.CALIBRATING]

    assert progress[0] < 0.2
    assert progress[-1] == pytest.approx(1.0, abs=0.05)
    assert all(b >= a - 1e-9 for a, b in zip(progress, progress[1:], strict=False))


def test_flags_presence_without_retuning():
    """Phase 4 exit criterion: empty room, then walking, then empty again — one calibration,
    no parameter changes in between."""
    history, _ = run_scenario(
        lambda t: 1.0 if 60.0 <= t < 100.0 else 0.0,
        140.0,
    )

    assert states_between(history, 40, 58) == {State.ABSENT}
    assert states_between(history, 70, 98) == {State.PRESENT}
    assert states_between(history, 115, 140) == {State.ABSENT}


def test_motion_index_separates_the_two_states_by_a_wide_margin():
    """A margin of a few percent would be a coin flip in a real room. There should be orders of
    magnitude between an empty room and someone walking through the link."""
    history, _ = run_scenario(lambda t: 1.0 if 60.0 <= t < 100.0 else 0.0, 110.0)

    empty = np.median([r.motion for t, r in history if 40 <= t < 58])
    walking = np.median([r.motion for t, r in history if 70 <= t < 98])

    assert walking > 20 * empty, f"empty {empty:.2f} vs walking {walking:.2f}"


def test_hysteresis_thresholds_are_ordered():
    _, detector = run_scenario(lambda t: 0.0, 45.0)
    result = detector.update(FrameRing(64, 128))  # no data; thresholds are already fixed

    assert detector._exit < detector._enter, "exit above enter would latch permanently"
    assert result is None


def test_agc_steps_alone_do_not_trigger_presence():
    """The false positive this whole design exists to prevent: gain steps every two seconds in
    an empty room must not read as someone walking around."""
    scene = Scene(SceneConfig(rate_hz=RATE, agc_period_s=2.0, agc_step_db=6.0, seed=4))
    pre = Preprocessor()
    ring = FrameRing(64, 12000)
    detector = PresenceDetector()

    saw_present = False
    for i in range(int(120 * RATE)):
        frame = scene.next_frame()
        if frame is None:
            continue
        ring.push(pre.process(frame))
        if i % UPDATE_EVERY == 0:
            result = detector.update(ring)
            if result is not None and result.state is State.PRESENT:
                saw_present = True

    assert not saw_present, "AGC steps were read as motion"


def test_debounce_rejects_a_single_noisy_window():
    """One window over threshold is noise. The state must not flip on it."""
    config = PresenceConfig(debounce_s=3.0)
    history, _ = run_scenario(
        lambda t: 3.0 if 60.0 <= t < 60.5 else 0.0,  # a half-second blip
        80.0,
        config=config,
    )

    assert State.PRESENT not in {r.state for _, r in history}


def test_recalibrate_returns_to_calibrating():
    _, detector = run_scenario(lambda t: 0.0, 45.0)
    assert detector.state is State.ABSENT

    detector.recalibrate()
    assert detector.state is State.CALIBRATING
    assert detector.baseline_var is None


def test_pca_and_plain_variance_both_work():
    """Both aggregations must land on ~1.0 in an empty room, or the calibrated thresholds mean
    different things depending on a checkbox."""
    for use_pca in (True, False):
        history, _ = run_scenario(
            lambda t: 1.0 if 60.0 <= t < 90.0 else 0.0,
            100.0,
            config=PresenceConfig(use_pca=use_pca),
        )
        assert states_between(history, 40, 58) == {State.ABSENT}, f"pca={use_pca}"
        assert State.PRESENT in states_between(history, 70, 88), f"pca={use_pca}"
