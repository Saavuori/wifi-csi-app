"""Measure breathing accuracy on real CSI with a known answer.

Sweeps a set of respiration rates, superimposes each on the same real recording (see
`inject_breathing`), pushes the result through the *actual* server pipeline — the same
`Hub.handle_datagram`, preprocessing, subcarrier selection and estimator the live app uses —
and reports the error against the rate that was injected.

This is the first accuracy figure in this project that is not measured against its own
simulator. What it is not is a claim about people: the coupling amplitude is chosen, not
observed, so the honest reading is "a respiration signal of this size, on this hardware's
noise, is recovered to within X breaths/min". Whether a given person at a given distance
produces a signal of that size is a separate question that needs a person.

    python -m csi.tools.eval_breathing recording.csi
    python -m csi.tools.eval_breathing recording.csi --amplitudes 0.30 0.18 0.10
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from ..config import Settings
from ..hub import Hub
from ..protocol import encode_frame, iter_records, parse_frame
from .inject_breathing import chest_path, frequencies, quantize_int8

DEFAULT_RATES = (10.0, 12.0, 14.0, 16.0, 20.0)


def load(path: Path, limit: int) -> list:
    frames = []
    with path.open("rb") as fp:
        for _offset, datagram in iter_records(fp):
            frames.append(parse_frame(datagram))
            if limit and len(frames) >= limit:
                break
    return frames


def estimate(frames: list, bpm: float, amplitude: float, settings: Settings) -> dict | None:
    """Inject `bpm` into a copy of `frames`, run the real pipeline, return its metrics."""
    hub = Hub(settings)
    freqs = frequencies(frames[0].n_sub, 2.437e9)
    t0 = frames[0].timestamp

    for frame in frames:
        csi = frame.complex()
        rms = float(np.sqrt(np.mean(np.abs(csi) ** 2)))
        if rms > 0:
            csi = csi + amplitude * rms * chest_path(
                freqs, (frame.timestamp - t0) / 1e6, bpm=bpm, chest_mm=5.0, distance_m=5.2
            )
        # A copy per run: the same Frame object is reused across rates, and mutating it would
        # inject each rate on top of the last.
        clone = parse_frame(encode_frame(frame))
        clone.data = quantize_int8(csi)
        hub.handle_datagram(encode_frame(clone), clone.timestamp / 1e6)

    state = next(iter(hub.nodes.values()), None)
    if state is None:
        return None
    history = hub.history_for(state)
    if history is None:
        return None
    return hub.compute_metrics(state, history)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="csi.tools.eval_breathing")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--rates", type=float, nargs="+", default=list(DEFAULT_RATES))
    parser.add_argument("--amplitudes", type=float, nargs="+", default=[0.18])
    parser.add_argument("--frames", type=int, default=6000,
                        help="frames to use; must comfortably exceed the analysis window")
    args = parser.parse_args(argv)

    frames = load(args.recording, args.frames)
    if not frames:
        raise SystemExit(f"no frames in {args.recording}")
    span_s = (frames[-1].timestamp - frames[0].timestamp) / 1e6
    print(f"{len(frames)} frames, {frames[0].n_sub} subcarriers, "
          f"{span_s:.0f}s at {len(frames)/max(span_s,1e-9):.1f} Hz\n")

    settings = Settings()
    settings.web_dir = None
    settings.record = False

    overall: list[float] = []
    for amplitude in args.amplitudes:
        print(f"chest amplitude {amplitude} of frame RMS")
        print(f"  {'true':>6}  {'est':>7}  {'error':>7}  {'conf':>5}  {'snr':>6}  note")
        errors: list[float] = []
        for bpm in args.rates:
            metrics = estimate(frames, bpm, amplitude, settings)
            breathing = (metrics or {}).get("breathing")
            rejected = (metrics or {}).get("breathing_rejected")
            if breathing is None or not breathing.get("bpm"):
                print(f"  {bpm:6.1f}  {'-':>7}  {'-':>7}  {'-':>5}  {'-':>6}  "
                      f"{rejected or 'no estimate'}")
                continue
            est = float(breathing["bpm"])
            err = est - bpm
            errors.append(abs(err))
            print(f"  {bpm:6.1f}  {est:7.2f}  {err:+7.2f}  "
                  f"{breathing['confidence']:5.2f}  {breathing['snr_db']:6.1f}  ")
        if errors:
            mae = sum(errors) / len(errors)
            overall.extend(errors)
            print(f"  MAE {mae:.2f} breaths/min over {len(errors)}/{len(args.rates)} rates\n")
        else:
            print("  no estimates produced\n")

    if overall:
        mae = sum(overall) / len(overall)
        worst = max(overall)
        print(f"overall MAE {mae:.2f} breaths/min, worst {worst:.2f}, "
              f"n={len(overall)}")
    else:
        print("no estimate was produced at any rate or amplitude")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
