# WiFi CSI sensing

Capture Channel State Information from ESP32 nodes, stream it to a server, visualize and analyze
it in a web app.

**Design principle: the pipeline is source-agnostic.** A frame is `(timestamp_us, node_id,
complex[N])` with `N` variable and explicit per frame. An ESP32 in HT20, the same board in HT40,
a future Raspberry Pi running Nexmon at 256 subcarriers, and a replayed public dataset are all
just producers into that format. Nothing downstream knows or cares which it is looking at.

```
  ESP32-S3 (TX)  ──100 Hz──►  air  ──►  ESP32-S3 (RX)  ──UDP──►  server  ──WebSocket──►  browser
                                          promiscuous              ingest                 waterfall
                                          CSI callback             record  ◄──replay──►   motion
                                                                   analyse                breathing
```

| Directory | What it is |
|---|---|
| `firmware/` | ESP-IDF project for the nodes. One image, two roles. |
| `server/` | Python: UDP ingest, recorder, replayer, DSP, HTTP + WebSocket. |
| `web/` | TypeScript + canvas front end. No framework. |
| `docs/` | Wire formats. |
| `deploy/` | Container, compose and reverse-proxy configuration. |

## Quick start, on a Raspberry Pi

64-bit Raspberry Pi OS on a Pi 4 or 5:

```sh
curl -fsSL https://raw.githubusercontent.com/Saavuori/wifi-csi-app/main/install.sh | bash
```

That installs Docker if it is missing, tunes the one kernel setting that matters for UDP
ingest, starts the server, and starts a synthetic node alongside it — so the waterfall moves
and the breathing view converges on a known answer without any hardware. Then open
`http://<pi>:8080`. Pass `--no-demo` when you have boards to point at it instead, and
`--uninstall` to remove it. Details and the compose file are in [`deploy/README.md`](deploy/README.md).

## Quick start, without hardware

The whole stack runs against a synthetic node whose channel model is physical enough that the
answers are known — a specific breathing rate, a specific motion schedule. That is how the DSP
was developed and how it is tested.

```sh
python -m venv .venv && .venv/bin/pip install -e "server[dev]"
cd web && npm install && npm run build && cd ..

# terminal 1
CSI_WEB_DIR=web/dist .venv/bin/python -m csi

# terminal 2 — two nodes, 14 breaths/min, alternating empty and occupied
.venv/bin/python -m csi.tools.synth_node --nodes 2 --breathing 14 --scenario cycle
```

Open <http://localhost:8080>. Within about a minute the presence detector finishes calibrating
and starts tracking the scenario, and the breathing view settles near 14.

For front-end work, `cd web && npm run dev` proxies `/api` and `/ws` to the Python server on
8080 and gives you hot reload.

## Quick start on Windows, with the boards already flashed

Same command, different path separator — the venv puts its interpreter in `Scripts\` rather than
`bin/`, and `python -m csi` is the same module either way:

```sh
CSI_WEB_DIR=web/dist CSI_ECHO_PORT=5568 .venv/Scripts/python.exe -m csi
```

`CSI_ECHO_PORT` is read only on the branch that ships the station firmware; see the note at the
end of the next section. It is harmless here and required there.

Three things decide whether frames actually arrive, and each fails silently in its own way:

- **The server must listen on the address the node was flashed with.** `CSI_SERVER_HOST` is
  baked into the image; check it against `ipconfig` before blaming the radio. There is no
  discovery and there is deliberately no fallback.
- **`CSI_ECHO_PORT` must be set when the nodes were built with `CSI_PROBE_UDP_ECHO`.** A station
  node generates its own CSI by probing and reporting the reply, so with nothing answering the
  probes the rate collapses to whatever incidental traffic the link carries — about 0.1 Hz here,
  which looks exactly like a dead node without saying so. The responder is off by default because
  a port that reflects whatever it is sent should not be open unless something needs it.
- **The wire version must match the firmware.** `CSI_WIRE_VERSION` is pinned on both ends and the
  server rejects anything else, one `unsupported version N` warning per datagram. That log line
  is the diagnostic: the boards are fine, the branch is wrong.

Verified against two ESP32-S3 nodes on 2026-07-31: node 11 held **94.7 Hz with 0.00% loss and
zero gaps over 26,471 frames**, 64 subcarriers, RSSI −82 dBm, 5.2 ms jitter, no reboots and no
roams. The waterfall reacts to an arm wave within a frame or two, which is the phase-3 exit
criterion.

![The waterfall on live CSI](docs/screenshot-live.png)

The horizontal bands with no data are the guard and DC subcarriers — HT20 carries 64 bins but
only 52 of them are populated, so an empty stripe around index 32 is the format, not a fault.

![Node health](docs/screenshot-node-health.png)

Node 10 is the same link seen from the other mesh access point and idles below 1 Hz, so it shows
as offline. Selecting a node in the header is what picks the link the analysis runs on.

## With hardware

Read `firmware/README.md` first — it lists the handful of settings that decide whether the
capture works at all, and why. In short: flash one board as transmitter and one as receiver,
put the transmitter's MAC in the receiver's `CSI_PEER_MAC`, point `CSI_SERVER_HOST` at this
server, and watch the Node health view for a stable rate and sub-1% loss.

The single-node station topology the screenshots above were taken on — one board joined to an
existing mesh, probing the access point rather than a second board — lives on
`claude/esp32-mesh-wifi-single` and is not merged here yet. It carries the UDP echo responder
and wire version 2; this branch is version 1 and will reject its frames outright. Run the server
from that worktree until the two are merged.

## The phases

Numbered as in the build plan.

| Phase | Where | State |
|---|---|---|
| 1 — Firmware | `firmware/` | Implemented; the ring and wire layout have host tests, and the radio path has now run on boards at 94.7 Hz with zero loss |
| 2 — Ingest + recorder | `server/csi/{ingest,recorder,replay,sessions}.py` | Implemented and tested |
| 3 — Waterfall | `web/src/views/waterfall.ts` | Implemented |
| 4 — Motion + presence | `server/csi/dsp/{presence,selection}.py` | Implemented and tested |
| 5 — Breathing | `server/csi/dsp/vitals.py` | Implemented and tested |
| 6 — Heart rate | same, different band | Implemented; expect it to work only at close range on a motionless subject |

### Exit criteria, and how to check them

- **Phase 1** — "stable ~80 Hz, sequence gaps under 1% over ten minutes, board stays cool."
  The first two are on the Node health view, measured continuously from device timestamps. Met
  on hardware: 94.7 Hz and 0.00% loss across 26,471 frames, on a board that had been up 5h38m
  with no reboots. The ten-minute soak and the thermal check are still worth doing on a board
  you intend to leave running.
- **Phase 2** — "a recording replays byte-identically through the live pipeline." This is a
  property of the format rather than something to keep re-testing: recordings store the raw
  datagrams, and the replayer hands the same bytes to the same parser the UDP listener uses.
  Asserted by `test_replay_is_byte_identical_to_what_was_recorded`.
- **Phase 3** — "wave your arm; if the waterfall does not visibly react, stop and fix the
  capture chain."
- **Phase 4** — "flags presence/absence across the empty-room and walking recordings with no
  manual retuning between them." Asserted by `test_flags_presence_without_retuning`.

## What the analysis actually does

The parts worth knowing about before trusting a number:

**AGC removal comes first.** The receiver's automatic gain control steps every few seconds, and
when it does every subcarrier's amplitude jumps by the same factor at once — indistinguishable
from major motion to a raw variance detector. Two independent defenses: normalize the frame
against reported power, and flag the transition using its uniformity signature (a large change
that is *uniform across subcarriers* is the receiver; real motion is frequency-selective).
Flagged frames are excluded from variance accumulation, not merely rescaled.

**Subcarrier selection is a gate, then a rank.** Ranking by raw variance promotes deep-fade
carriers whose variance is thermal noise. Ranking by mean-over-variance is worse — it is
maximized by a carrier that never changes. So: drop the bottom quantile by mean amplitude
(low magnitude *is* the fade signature), then rank by a metric specific to the task —
`var_occupied / var_baseline` for motion, in-band-over-out-of-band power for breathing.

**Window length dominates the breathing estimate.** Published MAE goes from 0.61 breaths/min at
a 1 s window to 0.09 at 20 s. It is a slider in the UI because the effect is large enough to
watch happen.

**Everything is computed server-side, identically for live and replayed frames.** That is what
makes a recording a faithful stand-in for the room, and it is why the recorder exists before
any of the analysis does.

## Two things measured during development

Both are commented at the code that depends on them, and both were found by running the
pipeline rather than by reading it:

- The subcarrier ranker needs Welch segments that **resolve** the band, not merely cover it.
  Welch's default segmenting gives ~0.2 Hz bins against a 0.4 Hz-wide breathing band, which
  puts the whole band in two bins and makes the ranking a coin flip.
- The smoothed-RSSI time constant must sit well **below** the respiration band. An EMA does not
  remove RSSI's 1 dB quantization noise, it low-pass filters it — so a 3 s time constant parks
  the residual at 0.33 Hz, directly on top of the breathing peak. Moving it to 30 s took
  synthetic breathing MAE from 1.70 to 0.29 breaths/min.

## Tests

```sh
.venv/bin/python -m pytest server/tests      # 115 tests
firmware/scripts/run_host_tests.sh           # ring buffer + wire layout, no hardware needed
cd web && npm run build                      # typecheck + bundle
```

All three run in CI on every push, and the container image is built for amd64 and arm64 only
after they pass — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Configuration

Environment variables, all optional:

| Variable | Default | Notes |
|---|---|---|
| `CSI_UDP_PORT` | 5566 | Where nodes send frames |
| `CSI_HTTP_PORT` | 8080 | API and web app |
| `CSI_DATA_DIR` | `./data` | Recordings and `sessions.json` |
| `CSI_WEB_DIR` | `../web/dist` | Built front end; unset serves the API only |
| `CSI_RECORD` | `true` | Auto-start a recording at boot. Losing a session is far more expensive than the disk — a node at 80 Hz writes about 1 GB/day |
| `CSI_HISTORY_S` | 120 | In-memory history per node |
| `CSI_METRICS_HZ` | 5 | Analysis rate. Runs whether or not a browser is connected |

## What this will and won't do

**Will:** motion detection, room-level presence, breathing rate at tuned positions, an overnight
activity record, a genuinely nice CSI visualization tool.

**Won't:** localization, direction, reliable multi-person counting, pose. Those need multiple
antennas on one radio or a much denser mesh. Single-link amplitude-only is one scalar view of
the room.

Two separate ESP32s do **not** give CSI-ratio benefits. That trick cancels carrier frequency
offset because both antennas share one oscillator; separate boards have independent clocks.
This is an amplitude-only system throughout — there is no phase unwrapping anywhere, on purpose.

**Deferred upgrades:** HT40 for 128 subcarriers, a plane reflector behind the PIFA, a Raspberry
Pi + Nexmon node for 256 subcarriers at 80 MHz. All three drop into the same ingest format
without touching the web app — `n_sub` is per-frame and the subcarrier layout tables are keyed
on it.

## Reference material

- Ma, Zhou & Wang — *WiFi Sensing with Channel State Information: A Survey*, ACM CSUR 52(3), 2019
- Kocheta, Bhatia & Obraczka — *PulseFi*, arXiv:2510.24744, 2025 — source of the filter parameters
- Wang et al. — *Human respiration detection with commodity WiFi devices*, UbiComp 2016 — Fresnel zones
- Zeng et al. — *FarSense*, IMWUT 2019 — the CSI-ratio model this hardware cannot use
- Strohmayer & Kampel — *Directional Antenna Systems for Long-Range Through-Wall HAR*, arXiv:2401.01388
- Hernandez & Bulut — *WiFi Sensing on the Edge*, IEEE COMST 2022
- `espressif/esp-csi`, `StevenMHernandez/ESP32-CSI-Tool`
