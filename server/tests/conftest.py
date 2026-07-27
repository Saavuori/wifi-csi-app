from __future__ import annotations

import numpy as np
import pytest

from csi.dsp.preprocess import PreprocessConfig, Preprocessor
from csi.ring import FrameRing
from csi.synth import Scene, SceneConfig


@pytest.fixture
def build_ring():
    """Run a synthetic scene through the real preprocessing path into a ring.

    Tests that want a signal with a known answer use this rather than hand-built arrays, so
    they exercise the same code the server does — masking, AGC handling, quantization to int8
    and all.
    """

    def _build(
        duration_s: float = 60.0,
        *,
        motion=0.0,
        norm_mode: str = "hybrid",
        capacity: int | None = None,
        **scene_kwargs,
    ) -> tuple[FrameRing, Scene]:
        config = SceneConfig(**scene_kwargs)
        scene = Scene(config)
        pre = Preprocessor(PreprocessConfig(norm_mode=norm_mode))
        ring = FrameRing(config.n_sub, capacity or int(duration_s * config.rate_hz * 1.2) + 64)

        n = int(duration_s * config.rate_hz)
        for i in range(n):
            level = motion(i / config.rate_hz) if callable(motion) else motion
            scene.set_motion(level)
            frame = scene.next_frame()
            if frame is not None:
                ring.push(pre.process(frame))
        return ring, scene

    return _build


@pytest.fixture(autouse=True)
def _quiet_numpy():
    """Any NaN reduction warning in a test is a bug in the masking, not noise to tolerate."""
    with np.errstate(all="raise"):
        yield
