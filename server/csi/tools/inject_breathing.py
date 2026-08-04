"""Add a chest reflection with a known rate to a real recording.

The problem this solves: every accuracy number this project has came from `synth.py`, which
generates the CSI *and* encodes the assumptions the DSP makes about it. That is circular — it
shows the estimator is self-consistent, not that it works on a radio.

This takes the other half from hardware. A recording from a real node carries the int8
quantisation, the timing jitter, the guard-band residue, the subcarrier fading and the
interference of an actual room — everything the simulator has to guess at — and this adds the
one thing it does not contain: a chest, moving at a rate we chose. Ground truth exact, nuisance
real.

The physics matches `Scene.channel_response` deliberately: a path whose length changes by twice
the chest displacement, which is what makes respiration frequency-selective across subcarriers.
A different model here would measure the difference between two of our own opinions.

Two honest limits. The coupling amplitude is an assumption, so this cannot tell you whether a
person at a given range and posture is detectable — it answers the narrower question of whether
a respiration signal of known size and rate is *recoverable* from real hardware noise. And the
result is re-quantised to int8 on the way out, exactly as the node would send it, so a
modulation too small to survive int8 will vanish here — which is itself worth knowing.

    python -m csi.tools.inject_breathing in.csi out.csi --bpm 14
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np

from ..protocol import REC_MAGIC, encode_frame, iter_records, parse_frame, record_bytes

C = 299_792_458.0
# 20 MHz OFDM spacing. The absolute centre matters far less than the spacing: it is the
# difference in phase *across* subcarriers that makes the modulation frequency-selective.
SUBCARRIER_SPACING_HZ = 312_500.0


def frequencies(n_sub: int, centre_hz: float) -> np.ndarray:
    """Absolute frequency per subcarrier, in this project's index convention (index 0 is DC)."""
    i = np.arange(n_sub)
    k = np.where(i < n_sub // 2, i, i - n_sub)
    return centre_hz + k * SUBCARRIER_SPACING_HZ


def chest_path(
    freqs: np.ndarray, t_s: float, *, bpm: float, chest_mm: float, distance_m: float
) -> np.ndarray:
    """Unit-amplitude reflection off a chest displaced sinusoidally at `bpm`."""
    disp_m = chest_mm * 1e-3 * math.sin(2 * math.pi * (bpm / 60.0) * t_s)
    # Out and back, so the path length changes by twice the displacement.
    d = distance_m + 2.0 * disp_m
    return np.exp(-2j * math.pi * freqs * d / C)


def quantize_int8(csi: np.ndarray) -> np.ndarray:
    """Complex CSI back to the interleaved (imag, real) int8 the wire carries."""
    out = np.empty(2 * csi.size, dtype=np.int8)
    out[0::2] = np.clip(np.rint(csi.imag), -127, 127).astype(np.int8)
    out[1::2] = np.clip(np.rint(csi.real), -127, 127).astype(np.int8)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="csi.tools.inject_breathing",
        description="Superimpose a known respiration signal on a real CSI recording.",
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--bpm", type=float, default=14.0)
    parser.add_argument(
        "--amplitude", type=float, default=0.18,
        help="chest path amplitude relative to each frame's RMS (default 0.18, matching "
             "synth.py's chest path against its direct path)",
    )
    parser.add_argument("--chest-mm", type=float, default=5.0)
    parser.add_argument("--distance-m", type=float, default=5.2)
    parser.add_argument("--centre-hz", type=float, default=2.437e9)
    parser.add_argument("--limit", type=int, default=0, help="stop after N frames")
    parser.add_argument("--node-id", type=int, default=0,
                        help="rewrite the node id, so an injected copy is distinguishable")
    args = parser.parse_args(argv)

    freqs: np.ndarray | None = None
    t0_us: int | None = None
    written = 0
    clipped_frames = 0

    with args.source.open("rb") as src, args.dest.open("wb") as dst:
        dst.write(REC_MAGIC)
        for _offset, datagram in iter_records(src):
            frame = parse_frame(datagram)
            if freqs is None:
                freqs = frequencies(frame.n_sub, args.centre_hz)
            if t0_us is None:
                t0_us = frame.timestamp

            csi = frame.complex()
            rms = float(np.sqrt(np.mean(np.abs(csi) ** 2)))
            if rms > 0:
                added = args.amplitude * rms * chest_path(
                    freqs,
                    (frame.timestamp - t0_us) / 1e6,
                    bpm=args.bpm,
                    chest_mm=args.chest_mm,
                    distance_m=args.distance_m,
                )
                csi = csi + added
                if np.abs(csi.real).max() > 127 or np.abs(csi.imag).max() > 127:
                    clipped_frames += 1

            if args.node_id:
                frame.node_id = args.node_id
            frame.data = quantize_int8(csi)
            dst.write(record_bytes(encode_frame(frame)))
            written += 1
            if args.limit and written >= args.limit:
                break

    print(f"{written} frames -> {args.dest}  ({args.bpm} BPM, amplitude {args.amplitude})")
    if clipped_frames:
        # Clipping distorts the very thing being measured, so it is reported rather than
        # buried: a run with many clipped frames is not a valid measurement.
        print(f"warning: {clipped_frames} frames clipped ({100*clipped_frames/written:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
