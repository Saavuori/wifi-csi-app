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


# How often to ask the hub for metrics, in frames. The live server polls on a timer; the gate
# publishes only once a run of estimates spanning `stability_window_s` agrees, so a harness that
# computes metrics once at the end sees a single estimate and is told "settling" forever.
_POLL_EVERY = 200


def run_once(
    frames: list, bpm: float, amplitude: float, settings: Settings
) -> tuple[list[float], int, str | None]:
    """Inject `bpm` and replay through the real pipeline, polling as the server does.

    Returns (published estimates, polls, last rejection). Polling rather than computing once at
    the end is not a detail: the stability gate deliberately refuses to answer from a single
    window, so a one-shot harness measures nothing but its own impatience.
    """
    hub = Hub(settings)
    freqs = frequencies(frames[0].n_sub, 2.437e9)
    t0 = frames[0].timestamp
    published: list[float] = []
    polls = 0
    rejection: str | None = None

    for i, frame in enumerate(frames):
        csi = frame.complex()
        rms = float(np.sqrt(np.mean(np.abs(csi) ** 2)))
        if rms > 0 and amplitude > 0:
            csi = csi + amplitude * rms * chest_path(
                freqs, (frame.timestamp - t0) / 1e6, bpm=bpm, chest_mm=5.0, distance_m=5.2
            )
        # A copy per run: the same Frame object is reused across rates, and mutating it would
        # inject each rate on top of the last.
        clone = parse_frame(encode_frame(frame))
        clone.data = quantize_int8(csi)
        hub.handle_datagram(encode_frame(clone), clone.timestamp / 1e6)

        if i % _POLL_EVERY != _POLL_EVERY - 1:
            continue
        state = next(iter(hub.nodes.values()), None)
        if state is None:
            continue
        history = hub.history_for(state)
        if history is None:
            continue
        polls += 1
        metrics = hub.compute_metrics(state, history) or {}
        breathing = metrics.get("breathing")
        if breathing and breathing.get("bpm"):
            published.append(float(breathing["bpm"]))
        else:
            rejection = metrics.get("breathing_rejected") or rejection

    return published, polls, rejection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="csi.tools.eval_breathing")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--rates", type=float, nargs="+", default=list(DEFAULT_RATES))
    parser.add_argument("--amplitudes", type=float, nargs="+", default=[0.18])
    parser.add_argument("--frames", type=int, default=16000,
                        help="frames to use. Must cover the analysis window plus the stability "
                             "gate's agreement period with room to spare — at 40 Hz that is "
                             "150 s before a single estimate can be published, so a short run "
                             "reports 'settling' and looks like a fault (default 16000, ~7 min)")
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
        print(f"chest amplitude {amplitude} of frame RMS"
              + ("   (no signal: anything reported here is a false positive)"
                 if amplitude == 0 else ""))
        print(f"  {'true':>6}  {'mean est':>9}  {'error':>7}  {'sd':>5}  {'shown':>6}  note")
        errors: list[float] = []
        for bpm in args.rates:
            published, polls, rejection = run_once(frames, bpm, amplitude, settings)
            shown = f"{100 * len(published) / max(polls, 1):.0f}%"
            if not published:
                print(f"  {bpm:6.1f}  {'-':>9}  {'-':>7}  {'-':>5}  {shown:>6}  "
                      f"{rejection or 'withheld'}")
                continue
            mean = float(np.mean(published))
            err = mean - bpm
            # Averaged over the run rather than taken from the last poll: one poll is one
            # window, and the whole point of the gate is that a single window is not evidence.
            if amplitude > 0:
                errors.append(abs(err))
            print(f"  {bpm:6.1f}  {mean:9.2f}  {err:+7.2f}  {np.std(published):5.2f}  "
                  f"{shown:>6}  ")
        if errors:
            mae = sum(errors) / len(errors)
            overall.extend(errors)
            print(f"  MAE {mae:.2f} breaths/min over {len(errors)}/{len(args.rates)} rates\n")
        else:
            print()

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
