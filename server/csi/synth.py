"""A synthetic CSI node.

This exists so the entire pipeline — ingest, recording, replay, DSP, the waterfall — can be
built and tested before any hardware arrives, and so the tests have a signal with a *known*
answer. If the breathing estimator says 14.0 BPM on a scene generated at 14.0 BPM, that is
worth more than any amount of staring at a real waterfall.

The channel model is small but physical rather than hand-drawn: a direct path plus a handful of
static scatterers plus one moving reflector, each contributing a complex exponential whose
phase depends on path length and subcarrier frequency. Two properties fall out of that
construction rather than being painted on, and both matter for testing:

  * breathing modulation is *frequency-selective* — different subcarriers see different
    amounts of it, because the moving path interferes constructively at some frequencies and
    destructively at others. That is the Fresnel-zone effect, and it is what makes subcarrier
    selection worth doing at all.
  * an AGC step is *uniform* across subcarriers, because it is applied after the channel.

A generator that produced a clean sinusoid on every subcarrier would let a broken selector and
a working one score identically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .dsp.subcarriers import index_to_k
from .protocol import Frame

C = 299_792_458.0
SUBCARRIER_SPACING = 312_500.0


@dataclass(slots=True)
class Path:
    """One propagation path.

    `chest_coupling` scales how much of the chest displacement this path picks up: 0 for a path
    that misses the subject entirely, 1 for a clean reflection off the sternum.
    """

    amplitude: float
    distance_m: float
    chest_coupling: float = 0.0
    velocity_ms: float = 0.0


@dataclass(slots=True)
class SceneConfig:
    node_id: int = 1
    n_sub: int = 64
    rate_hz: float = 80.0
    centre_freq_hz: float = 2.437e9  # channel 6
    channel: int = 6

    breathing_bpm: float = 14.0
    heart_bpm: float = 0.0  # 0 disables; the cardiac path is ~20x weaker than respiration
    chest_amplitude_mm: float = 5.0
    heart_amplitude_mm: float = 0.3

    # Motion is modelled as a scatterer whose distance sweeps. `motion` scales its amplitude,
    # so 0.0 is an empty room and 1.0 is someone walking through the link.
    motion: float = 0.0
    motion_speed_ms: float = 1.2

    noise_db: float = -28.0  # relative to the direct path
    rssi_dbm: int = -52
    noise_floor_dbm: int = -92

    # Timing realism. Jitter is uniform in +-jitter_us; drop_rate randomly omits frames, which
    # exercises the sequence-gap accounting and the resampler at the same time.
    jitter_us: int = 400
    drop_rate: float = 0.0

    # AGC steps every so often, by a whole number of dB, as the real receiver does.
    agc_period_s: float = 12.0
    agc_step_db: float = 6.0

    seed: int = 0
    paths: list[Path] = field(default_factory=list)


def default_paths() -> list[Path]:
    """A direct path, three static scatterers, and one chest reflection.

    The chest path is weak (0.18 against a direct path of 1.0) on purpose. Respiration shows up
    as a small perturbation of a large static channel, and a generator that made it obvious
    would let a broken estimator pass.
    """
    return [
        Path(amplitude=1.0, distance_m=3.0),
        Path(amplitude=0.45, distance_m=4.6),
        Path(amplitude=0.30, distance_m=6.1),
        Path(amplitude=0.22, distance_m=8.4),
        Path(amplitude=0.18, distance_m=5.2, chest_coupling=1.0),
    ]


class Scene:
    """Generates Frames for a synthetic room. Deterministic given the seed."""

    def __init__(self, config: SceneConfig | None = None) -> None:
        self.config = config or SceneConfig()
        self.rng = np.random.default_rng(self.config.seed)
        self.paths = self.config.paths or default_paths()
        self.seq = 0
        self.t_us = 0
        self._agc_db = 0.0
        self._next_agc_us = int(self.config.agc_period_s * 1e6)

        k = index_to_k(self.config.n_sub)
        self.freqs = self.config.centre_freq_hz + k * SUBCARRIER_SPACING

    # -- scene control -------------------------------------------------------------------

    def set_motion(self, level: float) -> None:
        self.config.motion = float(level)

    def set_breathing(self, bpm: float) -> None:
        self.config.breathing_bpm = float(bpm)

    # -- generation ----------------------------------------------------------------------

    def channel_response(self, t_s: float) -> np.ndarray:
        """Complex channel across subcarriers at time `t_s`, shape (n_sub,)."""
        cfg = self.config
        h = np.zeros(cfg.n_sub, dtype=np.complex128)

        breath_phase = 2 * math.pi * (cfg.breathing_bpm / 60.0) * t_s
        heart_phase = 2 * math.pi * (cfg.heart_bpm / 60.0) * t_s

        for path in self.paths:
            d = path.distance_m
            if path.chest_coupling:
                # The chest moves; the reflected path length changes by twice the displacement
                # because the wave makes the trip out and back.
                disp = cfg.chest_amplitude_mm * math.sin(breath_phase)
                if cfg.heart_bpm > 0:
                    disp += cfg.heart_amplitude_mm * math.sin(heart_phase)
                d += 2.0 * disp * 1e-3 * path.chest_coupling
            d += path.velocity_ms * t_s
            h += path.amplitude * np.exp(-2j * math.pi * self.freqs * d / C)

        if cfg.motion > 0:
            # A scatterer sweeping back and forth through the link. Its distance changes by
            # metres rather than millimetres, so it dominates and smears across all subcarriers
            # — which is exactly what walking looks like on a waterfall.
            d = 4.0 + 1.5 * math.sin(2 * math.pi * 0.25 * t_s) + cfg.motion_speed_ms * 0.05 * t_s
            h += cfg.motion * 0.6 * np.exp(-2j * math.pi * self.freqs * d / C)

        return h

    def next_frame(self) -> Frame | None:
        """Advance one sample period. Returns None for a dropped frame (seq still advances)."""
        cfg = self.config
        period_us = 1e6 / cfg.rate_hz
        self.t_us += int(period_us)
        jitter = int(self.rng.integers(-cfg.jitter_us, cfg.jitter_us + 1)) if cfg.jitter_us else 0
        t_us = max(0, self.t_us + jitter)
        self.seq += 1

        if cfg.drop_rate > 0 and self.rng.random() < cfg.drop_rate:
            return None

        h = self.channel_response(t_us / 1e6)

        noise_scale = 10.0 ** (cfg.noise_db / 20.0)
        h += noise_scale * (
            self.rng.standard_normal(cfg.n_sub) + 1j * self.rng.standard_normal(cfg.n_sub)
        ) / math.sqrt(2)

        if self.t_us >= self._next_agc_us:
            self._next_agc_us += int(cfg.agc_period_s * 1e6)
            self._agc_db = 0.0 if self._agc_db else cfg.agc_step_db

        # Scale into the int8 range the ESP32 reports, then quantize. Quantization is part of
        # the model, not an accident: 8-bit CSI is what the hardware gives, and an algorithm
        # that only works on float64 is not going to survive contact with it.
        gain = (10.0 ** (self._agc_db / 20.0)) * 46.0 / max(np.abs(h).mean(), 1e-9)
        scaled = h * gain
        data = np.empty(2 * cfg.n_sub, dtype=np.int8)
        data[0::2] = np.clip(np.rint(scaled.imag), -128, 127)
        data[1::2] = np.clip(np.rint(scaled.real), -128, 127)

        # RSSI does not follow the AGC step. The radio reports received power *after*
        # compensating for its own gain, which is precisely why RSSI can be used to undo the
        # gain — and why the synthetic node must not cheat by moving it. The +-1 dBm dither is
        # the real quantization, and it is what makes pure RSSI normalization noisier than the
        # hybrid mode in preprocess.py.
        rssi = cfg.rssi_dbm + int(self.rng.integers(-1, 2))

        return Frame(
            node_id=cfg.node_id,
            seq=self.seq,
            timestamp=t_us,
            rssi=int(np.clip(rssi, -100, -20)),
            noise_floor=cfg.noise_floor_dbm,
            channel=cfg.channel,
            sec_channel=0,
            n_sub=cfg.n_sub,
            data=data,
        )

    def frames(self, duration_s: float) -> list[Frame]:
        """Generate a whole recording's worth at once. Used by tests and the fixture tool."""
        n = int(duration_s * self.config.rate_hz)
        out = []
        for _ in range(n):
            frame = self.next_frame()
            if frame is not None:
                out.append(frame)
        return out
