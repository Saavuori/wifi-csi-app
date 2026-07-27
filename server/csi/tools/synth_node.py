"""A synthetic ESP32, speaking the real wire format over a real UDP socket.

    python -m csi.tools.synth_node --nodes 2 --breathing 14 --scenario walk

The server cannot tell this apart from hardware, which is the point: the ingest path, the
recorder, the waterfall and the detectors can all be finished and tested before the boards
arrive, and every one of them is then exercised against a signal whose correct answer is known.
"""

from __future__ import annotations

import argparse
import math
import socket
import sys
import time

from ..protocol import encode_frame
from ..synth import Scene, SceneConfig


def build_scenes(args: argparse.Namespace) -> list[Scene]:
    scenes = []
    for i in range(args.nodes):
        cfg = SceneConfig(
            node_id=args.first_node_id + i,
            n_sub=args.subcarriers,
            rate_hz=args.rate,
            breathing_bpm=args.breathing,
            heart_bpm=args.heart,
            drop_rate=args.drop_rate,
            jitter_us=args.jitter_us,
            agc_period_s=args.agc_period,
            seed=args.seed + i,
        )
        scenes.append(Scene(cfg))
    return scenes


def motion_for(scenario: str, t: float) -> float:
    """Motion level over time, so a run exercises the presence detector's transitions."""
    if scenario == "empty":
        return 0.0
    if scenario == "walk":
        return 1.0
    if scenario == "cycle":
        # 40 s empty, 20 s occupied, repeating. Long enough for the 30 s calibration to
        # complete during the first quiet stretch.
        return 1.0 if (t % 60.0) >= 40.0 else 0.0
    if scenario == "ramp":
        return 0.5 * (1.0 + math.sin(2 * math.pi * t / 120.0))
    return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(prog="csi-synth", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5566)
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--first-node-id", type=int, default=1)
    parser.add_argument("--rate", type=float, default=80.0, help="frames per second per node")
    parser.add_argument("--subcarriers", type=int, default=64, choices=[64, 128, 256])
    parser.add_argument("--breathing", type=float, default=14.0, help="breaths per minute")
    parser.add_argument("--heart", type=float, default=0.0, help="BPM; 0 disables")
    parser.add_argument(
        "--scenario", default="cycle", choices=["empty", "walk", "cycle", "ramp"]
    )
    parser.add_argument("--drop-rate", type=float, default=0.002)
    parser.add_argument("--jitter-us", type=int, default=400)
    parser.add_argument("--agc-period", type=float, default=12.0)
    parser.add_argument("--duration", type=float, default=0.0, help="seconds; 0 runs forever")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    scenes = build_scenes(args)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)
    period = 1.0 / args.rate

    print(
        f"synthetic node(s) {[s.config.node_id for s in scenes]} -> udp://{args.host}:{args.port} "
        f"at {args.rate:g} Hz, {args.subcarriers} subcarriers, "
        f"breathing {args.breathing:g} BPM, scenario {args.scenario}",
        file=sys.stderr,
    )

    start = time.monotonic()
    sent = 0
    next_tick = start
    try:
        while True:
            now = time.monotonic()
            elapsed = now - start
            if args.duration and elapsed >= args.duration:
                break

            motion = motion_for(args.scenario, elapsed)
            for scene in scenes:
                scene.set_motion(motion)
                frame = scene.next_frame()
                if frame is None:
                    continue  # a dropped frame, exercising the gap accounting
                sock.sendto(encode_frame(frame), dest)
                sent += 1

            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                # Fell behind — resynchronize rather than accumulating debt and then bursting.
                next_tick = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        elapsed = time.monotonic() - start
        print(
            f"sent {sent} frames in {elapsed:.1f}s ({sent / max(elapsed, 1e-9):.1f} fps)",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
