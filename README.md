# WiFi CSI sensing

Capture Channel State Information from ESP32 nodes, stream it to a server, visualize and analyze
it in a web app.

**Design principle: the pipeline is source-agnostic.** A frame is `(timestamp_us, node_id,
complex[N])` with `N` variable and explicit per frame. An ESP32 in HT20, the same board in HT40,
a future Raspberry Pi running Nexmon at 256 subcarriers, and a replayed public dataset are all
just producers into that format. Nothing downstream knows or cares which it is looking at.

```
   mesh AP  ◄──ping 100 Hz──  ESP32-S3  ──UDP──►  server  ──WebSocket──►  browser
      │                       station           ingest                    waterfall
      └────echo reply────►    CSI callback      record  ◄──replay──►      motion
                                                analyse                   breathing
```

One board. It joins the WiFi you already have, pings the gateway at a fixed rate, and reports
CSI for each reply — so the link it measures is the one between the board and the access point,
and the sampling rate is a property of the node rather than of the household's traffic.

| Directory | What it is |
|---|---|
| `firmware/` | ESP-IDF project for the node. One image, three roles. |
| `server/` | Python: UDP ingest, recorder, replayer, DSP, HTTP + WebSocket. |
| `web/` | TypeScript + canvas front end. No framework. Works on a phone, which is where the placement tuner belongs. |
| `docs/` | Wire formats. |
| `deploy/` | Container and reverse-proxy configuration. |

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

The server listens on all interfaces, so any device on the same network reaches it at
`http://<server-ip>:8080`. The front end is laid out for a phone as well as a desktop — same
views, same canvases, same numbers, with the sidebar becoming a bottom tab bar. That matters for
the Placement view in particular: it exists to be watched while you are across the room holding
the node, and the number it shows has to be the real one. On Windows the firewall usually needs
an inbound rule for the interpreter running the server.

## With hardware

Read `firmware/README.md` first — it lists the handful of settings that decide whether the
capture works at all, and why. In short:

1. Flash one board with your SSID and `CSI_SERVER_HOST` pointing at this server.
2. Read the boot scan, or open the node's setup page and press **Scan**. Either lists every
   access point in range with channel and RSSI — on a mesh that is several, and the phone app
   does not show them to you.
3. Pick the one whose line to the node crosses the doorway, bed, or hallway you care about, and
   lock the node to its BSSID. **That choice is the placement decision.** The system senses along
   that line; the node is one end of it and you do not get to move the other. It is not the
   strongest access point that wins, it is the one with the right geometry.
4. Watch the Node health view for a stable rate, sub-1% loss, and `roams` at zero.

If `roams` climbs, the mesh is still moving the node between access points and every calibration
dies with each move. Fix that before trusting anything downstream of it.

**Settings live on the node, not in the image.** After the first flash, the board serves a
settings page — at its own address on your network, printed at boot, and on a `csi-setup-xxxxxx`
network of its own when it cannot reach yours. Changing network, access point, server address or
probe rate needs no toolchain and no cable, and reflashing keeps what you saved.

The firmware runs on the original ESP32 as well as the S3: `firmware/sdkconfig.defaults.esp32`
carries the target and the 4 MB flash layout, and no C changes are needed.

The two-board pair is still supported and described in the firmware README, but one board is the
default and the place to start.

## The phases

Numbered as in the build plan.

| Phase | Where | State |
|---|---|---|
| 1 — Firmware | `firmware/` | Implemented and **run on hardware**: 96–99 Hz, zero sequence gaps, 96–100% probe yield. See the measurements in `firmware/README.md` |
| 2 — Ingest + recorder | `server/csi/{ingest,recorder,replay,sessions}.py` | Implemented and tested |
| 3 — Waterfall | `web/src/views/waterfall.ts` | Implemented |
| 4 — Motion + presence | `server/csi/dsp/{presence,selection}.py` | Implemented and tested |
| 5 — Breathing | `server/csi/dsp/vitals.py` | Implemented and tested |
| 6 — Heart rate | same, different band | Implemented; expect it to work only at close range on a motionless subject |

### Exit criteria, and how to check them

- **Phase 1** — "stable ~80 Hz, sequence gaps under 1% over ten minutes, board stays cool."
  The first two are on the Node health view, measured continuously from device timestamps. With
  a single node the rate is only as steady as the access point's willingness to answer, so read
  it alongside the yield in the node's own statistics log: a rate of 60 Hz at a 100 Hz probe
  rate is the link being busy, not the board being slow.
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

**A hole in the stream is refused, not interpolated over.** `np.interp` cannot fail: hand it a
window with two seconds missing and it draws a straight line across, returns an array of exactly
the right length, and says nothing. A ramp lasting seconds has its fundamental in the 0.1–0.5 Hz
respiration band, so the estimator that follows produces a confident number describing the
network. Windows whose largest gap exceeds 0.5 s are declined with a reason instead. This is the
failure mode a node sharing an access point has and a dedicated transmitter pair does not.

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
.venv/bin/python -m pytest server/tests      # 122 tests
firmware/scripts/run_host_tests.sh           # ring buffer + wire layout, no hardware needed
cd web && npm run build                      # typecheck + bundle
```

## Configuration

Environment variables, all optional:

| Variable | Default | Notes |
|---|---|---|
| `CSI_UDP_PORT` | 5566 | Where nodes send frames |
| `CSI_HTTP_PORT` | 8080 | API and web app |
| `CSI_ECHO_PORT` | unset | Opens a UDP echo responder for station nodes built with `CSI_PROBE_UDP_ECHO`. Only needed when the router will not answer pings; a port that reflects whatever it is sent should not be open by default |
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

**The link is shared.** With one node the far end is an access point serving the whole house, so
the sample rate and the reply yield depend on how busy it is. Expect Phase 1's "gaps under 1%"
to be harder to hit in the evening than at 3 a.m., and read the yield next to the rate. A second
board — flashed as a transmitter, giving a link nobody else touches — remains the answer if that
turns out to matter, and the pipeline already supports it: nothing in the format or the analysis
knows how many nodes there are.

Two separate ESP32s do **not** give CSI-ratio benefits. That trick cancels carrier frequency
offset because both antennas share one oscillator; separate boards have independent clocks.
This is an amplitude-only system throughout — there is no phase unwrapping anywhere, on purpose.

**A mesh is not only a cost.** Several access points on one SSID means several candidate sensing
lines through the house, and you get to pick which one by choosing the BSSID to lock to. A
second node later, locked to a *different* access point, adds a second line for the price of a
board — the server has been multi-node since the first commit.

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
