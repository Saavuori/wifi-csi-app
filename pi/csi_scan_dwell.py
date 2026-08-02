#!/usr/bin/env python3
"""One channel of a monitor-mode sweep: who transmitted, how often, and how loud.

csi-scan-monitor.sh arms the extractor on one channel with no transmitter filter and runs this
for a couple of seconds. This counts the transmitters it hears and prints one line each:

    <bssid> <channel> <frames> <median rssi dBm>

Whitespace-separated text rather than JSON because the caller merges the channels with sort(1)
and builds the JSON itself, so a parser on the other side would earn nothing.

What this can and cannot see follows entirely from where the numbers come from, and a caller
that forgets it will present the result as something it is not:

  * BSSIDs, never SSIDs. The extractor only produces CSI for HT/VHT frames — the measurement is
    read out of the channel-estimate table and a legacy frame has only an L-LTF — and measured
    on this hardware with the transmitter filter removed entirely (25 s, nine access points
    beaconing in earshot at ~10 Hz each) not one beacon or management frame ever produced a
    packet. A network's name is only ever carried in a beacon or a probe response. The names are
    not missing here, they are unobtainable this way.
  * transmitters, not access points. A block ack from a laptop to its access point is an HT
    frame like any other, so clients appear in the list beside the radios. Nothing in the
    18-byte header separates them: it carries the first byte of the frame control field, which
    says "block ack" or "QoS data", not the ToDS/FromDS bits that would say which end sent it.
    Measuring a station's uplink is a perfectly good measurement, so they are reported rather
    than guessed away.

The decoding itself is imported from csi_node.py rather than repeated. That file already owns
the nexmon header offsets and has a comment explaining what each one cost to establish; a second
copy here would be the one that goes stale.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

try:
    from csi_node import (
        NEXMON_UDP_PORT,
        chanspec_channel,
        open_capture,
        parse_nexmon,
        udp_payload,
    )
except ImportError as exc:  # pragma: no cover - only ever hit on a broken install
    raise SystemExit(
        f"csi_scan_dwell.py has to sit next to csi_node.py, which owns the packet decoder ({exc})"
    ) from exc

# Per-transmitter RSSI samples kept for the median. A median over a few thousand samples is the
# same number as a median over all of them, and an unfiltered dwell on a busy channel can
# produce a great many; this bounds the memory without changing the answer.
MAX_SAMPLES = 4096

# Enough for a bcm43455c0 at 80 MHz (1042 bytes of payload) plus the IP and UDP headers.
RECV_SIZE = 4096


def _is_real_channel(ch: int) -> bool:
    return 1 <= ch <= 14 or 32 <= ch <= 177


def record(seen: dict, pkt, expect_channel: int = 0) -> None:
    """Add one packet to the tally, keyed on (transmitter, channel).

    `expect_channel` is the channel the caller armed the extractor on, and is used only when the
    packet's own chanspec decodes to something that is not a channel at all. Preferring the
    packet's value is deliberate: if the retune did not take, the frames really did arrive on
    another channel, and saying so is worth more than repeating what we asked for.
    """
    channel = chanspec_channel(pkt.chanspec)
    if not _is_real_channel(channel):
        channel = expect_channel
    entry = seen.get((pkt.src_mac, channel))
    if entry is None:
        entry = [0, []]
        seen[(pkt.src_mac, channel)] = entry
    entry[0] += 1
    if len(entry[1]) < MAX_SAMPLES:
        entry[1].append(pkt.rssi)


def rows(seen: dict) -> list[tuple[str, int, int, int]]:
    """The tally as (bssid, channel, frames, median rssi), busiest transmitter first."""
    out = []
    for (mac, channel), (frames, samples) in seen.items():
        rssi = int(round(statistics.median(samples))) if samples else 0
        out.append((":".join(f"{b:02x}" for b in mac), channel, frames, rssi))
    out.sort(key=lambda row: (-row[2], row[0]))
    return out


def dwell(iface: str, seconds: float, expect_channel: int = 0) -> dict:
    """Listen on `iface` for `seconds` and tally what transmitted.

    The socket is drained in a tight loop and nothing is done per packet beyond the decode, so a
    receive-buffer overrun on a busy channel is unlikely — and would not change the answer much
    if it happened, because the counts are only ever used to rank transmitters against each other
    within the same dwell.
    """
    sock = open_capture(iface)
    seen: dict[tuple[bytes, int], list] = {}
    deadline = time.monotonic() + seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                packet = sock.recv(RECV_SIZE)
            except TimeoutError:
                # socket.timeout under its 3.10 name: the dwell is up.
                break
            except InterruptedError:
                continue
            payload = udp_payload(packet, NEXMON_UDP_PORT)
            if payload is None:
                continue
            pkt = parse_nexmon(payload)
            if pkt is None:
                continue
            record(seen, pkt, expect_channel)
    finally:
        sock.close()
    return seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Count the transmitters heard on one channel. BSSIDs only — beacons carry "
                    "no CSI, so no SSID is available, and the list includes client stations as "
                    "well as access points.",
    )
    ap.add_argument("--iface", required=True, help="the capture radio, in monitor mode")
    ap.add_argument("--seconds", type=float, default=2.0, help="how long to listen (default 2)")
    ap.add_argument("--channel", type=int, default=0,
                    help="the channel the extractor was armed on; only used to label frames "
                         "whose own chanspec is unreadable")
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("error: a packet socket needs root", file=sys.stderr)
        return 1

    try:
        seen = dwell(args.iface, args.seconds, args.channel)
    except OSError as exc:
        print(f"error: cannot listen on {args.iface}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    heard = rows(seen)
    for mac, channel, frames, rssi in heard:
        print(f"{mac} {channel} {frames} {rssi}")

    # To the journal, not to stdout: the caller is assembling JSON out of the lines above.
    total = sum(row[2] for row in heard)
    print(f"channel {args.channel or '?'}: {total} frames from {len(heard)} transmitters "
          f"in {args.seconds:g} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
