# WiFi CSI sensing

Capture Channel State Information from ESP32 nodes, stream it to a server, visualize and analyze
it in a web app.

**Design principle: the pipeline is source-agnostic.** A frame is `(timestamp_us, node_id,
complex[N])` with `N` variable and explicit per frame. An ESP32 in HT20, the same board in HT40,
a future Raspberry Pi running Nexmon at 256 subcarriers, and a replayed public dataset are all
just producers into that format. Nothing downstream knows or cares which it is looking at.

```
   mesh AP  ◄─── probe, 100 Hz ───  ESP32-S3  ──CSI over UDP──►  server  ──WebSocket──►  browser
      │                             station                      ingest                  waterfall
      └───── reply ────────────►    CSI callback                 record  ◄──replay──►    motion
                                    on every reply               analyse                 breathing
```

**One board, doing both halves of the job.** It joins the WiFi you already have, sends a probe at
a fixed rate, and reports CSI for each reply — so it generates the traffic it measures. The link
it senses is the one between the board and the access point, and the sampling rate is a property
of the node rather than of the household's traffic. That last part is the whole point: a station
that only listens gets a CSI callback whenever the access point happens to address it, which on
an idle network is a few frames a second, and the spectrum you compute from that describes the
household's traffic pattern rather than the room.

The probe is an ICMP ping to the gateway by default, and a UDP round trip through the server
(`CSI_PROBE_UDP_ECHO` on the node, `CSI_ECHO_PORT` on the server) when the router will not answer
pings at rate — which, on the consumer mesh hardware this was built against, it would not. Either
way the CSI itself reaches the server over UDP.

| Directory | What it is |
|---|---|
| `firmware/` | ESP-IDF project for the node. One image, three roles. |
| `server/` | Python: UDP ingest, recorder, replayer, DSP, HTTP + WebSocket. |
| `web/` | TypeScript + canvas front end. No framework. Works on a phone, which is where the placement tuner belongs. |
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

The server listens on all interfaces, so any device on the same network reaches it at
`http://<server-ip>:8080`. The front end is laid out for a phone as well as a desktop — same
views, same canvases, same numbers, with the sidebar becoming a bottom tab bar. That matters for
the Placement view in particular: it exists to be watched while you are across the room holding
the node, and the number it shows has to be the real one. On Windows the firewall usually needs
an inbound rule for the interpreter running the server.

## Quick start on Windows, with a board already flashed

Same command, different path separator — the venv puts its interpreter in `Scripts\` rather than
`bin/`, and `python -m csi` is the same module either way:

```sh
CSI_WEB_DIR=web/dist CSI_ECHO_PORT=5568 .venv/Scripts/python.exe -m csi
```

Three things decide whether frames actually arrive, and each fails silently in its own way:

- **The server must listen on the address the node was flashed with.** `CSI_SERVER_HOST` is
  baked into the image until you change it from the node's setup page; check it against
  `ipconfig` before blaming the radio. There is no discovery and there is deliberately no
  fallback.
- **`CSI_ECHO_PORT` must be set when the node was built with `CSI_PROBE_UDP_ECHO`.** The node
  generates its own CSI by probing and reporting the reply, so with nothing answering the probes
  the rate collapses to whatever incidental traffic the link carries — about 0.1 Hz here, which
  looks exactly like a dead node without saying so. The responder is off unless the variable is
  set, because a port that reflects whatever it is sent should not be open unless something needs
  it. Leave it unset if the node probes by ICMP.
- **The wire version must match the firmware.** `CSI_WIRE_VERSION` is pinned on both ends — 2 as
  of the single-node topology — and the server rejects anything else, one `unsupported version N`
  warning per datagram. That log line is the diagnostic: the board is fine, the image is stale.

Measured here on 2026-07-31, with two station nodes on the mesh: node 11 held **94.7 Hz with
0.00% loss and zero gaps over 26,471 frames**, 64 subcarriers, RSSI −82 dBm, 5.2 ms jitter, no
reboots and no roams. The waterfall reacts to an arm wave within a frame or two, which is the
phase-3 exit criterion.

![The waterfall on live CSI](docs/screenshot-live.png)

The horizontal bands with no data are the guard and DC subcarriers — HT20 carries 64 bins but
only 52 of them are populated, so an empty stripe around index 32 is the format, not a fault.

![Node health](docs/screenshot-node-health.png)

Node 10 is the same link seen from the other mesh access point and idles below 1 Hz, so it shows
as offline. Selecting a node in the header is what picks the link the analysis runs on.

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
5. Read the **yield** in the node's own serial log — CSI frames over probes. It is the number
   that has no equivalent in the two-board topology, and the one that tells you whether the
   access point is holding up its end.

If `roams` climbs, the mesh is still moving the node between access points and every calibration
dies with each move. Fix that before trusting anything downstream of it.

**If the yield collapses, switch the probe to UDP echo.** Routers rate-limit ICMP, and this one
stopped answering entirely under sustained 100 Hz probing while still answering a laptop at 4 Hz
— the yield went to 0%, which reads exactly like a dead board. Rebuild the node with
`CSI_PROBE_UDP_ECHO` and start the server with `CSI_ECHO_PORT=5568`; that restored it to 96%, and
the yield is exact there because both ends are ours. Expect to need this on consumer mesh
hardware. The responder is off unless the variable is set, so setting one without the other is
the same collapse from the other direction.

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
| 1 — Firmware | `firmware/` | Implemented and **run on hardware**: 96–99 Hz, zero sequence gaps, 96–100% probe yield. The ring and wire layout also have host tests. See the measurements in `firmware/README.md` |
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
  rate is the link being busy, not the board being slow. Met on hardware: 94.7 Hz and 0.00% loss
  across 26,471 frames, on a board that had been up 5h38m with no reboots. The ten-minute soak
  and the thermal check are still worth doing on a board you intend to leave running.
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
.venv/bin/python -m pytest server/tests      # 129 tests
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
