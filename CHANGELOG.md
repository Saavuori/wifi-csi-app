# Changelog

Entries below this line are written by the release workflow from the pull request that caused
them. The version is decided by that PR's release type — one `release:` label, or a
conventional-commit title. See `.github/workflows/pr-release-type.yml`.

Versions are `MAJOR.MINOR.PATCH`:

| Release type | When | Example |
|---|---|---|
| `major` | a breaking change — the uplink wire format, an API removal, anything that makes an existing node or recording stop working | 1.4.2 → 2.0.0 |
| `minor` | a new capability that does not break what came before | 1.4.2 → 1.5.0 |
| `patch` | a fix, a performance change, an internal tidy-up | 1.4.2 → 1.4.3 |
| `skip` | docs, CI, tests — nothing a user of a release would notice | unchanged |

The git tag is authoritative. `server/pyproject.toml` and `web/package.json` are updated to
match, and the running server reports the same number on `/api/version` and in the header.

## v0.1.1 — 2026-08-04

fix: give the release-notes step the environment it needs (#28)

### Changes
- fix: give the release-notes step the environment it needs

Release type: **patch** (v0.1.0 → v0.1.1)

## v0.1.0 — 2026-08-03

feat: report the running build, automate versioning, and fill the screen with the waterfall (#27)

### Changes
- Report which build is running, and let the merged PR decide the version
- Make the installed helpers match what the hardware actually accepts
- Stop reporting an SNR that was never measured, and let the DC-leakage bin be dropped
- Let the waterfall fill the screen
- Write down what the bcm43455 actually hands up
- Recover from a firmware that stops delivering while reporting that it has not
- Stop the guard bands from setting the scale, and let a passive node follow the air
- Add UI control of node channel and traffic generation, plus a WiFi view
- Report the subcarrier count the loop hoisted it to report
- Use a format specifier in the status line, not percent format
- Say what the firmware actually does to an associated radio
- Provoke traffic from the wired side when the channel goes quiet
- Fix the nexmon header layout, and the silence that hid it
- Fix six things the first real Pi install found
- Build only this chip's firmware, not every chip in the tree
- Drop the python2.7 check; nothing on this build path uses python
- Remove every runtime path that can emit synthetic frames
- Always sense on a Pi, and never fabricate data
- Clear a node's history when its subcarrier mask changes
- A phone-first shell for the instrument, and an identity to match the site
- Read the kernel timestamp on a 32-bit Raspberry Pi too
- Size a ring slot from the wire header instead of by hand
- Do not fault reporting a ring the transmitter role never started
- Reject subcarriers with a hole in them at the magnitude gate
- Make a config patch total, so a typo cannot close the WebSocket
- Stop a replay promptly, and never let the stop path raise
- Pi node: keep the CSI build on our nexmon fork, resume or not
- README: one node, either kind — and a landing page that runs the instrument
- Raspberry Pi CSI node, via a nexmon_csi fork that keeps the Wi-Fi up
- README: describe the node that exists, and the probe that works
- Document bringing the app up locally against live boards
- CI, multi-arch images, and a one-command Raspberry Pi install
- Run the DSP off the event loop
- On-device settings, a setup page, and the first run on real hardware
- Single-node topology: one ESP32 on the home mesh
- Web layout: explicit columns, plus deployment config and README
- Web app: waterfall and the full view surface (phase 3)
- Firmware: ESP32-S3 CSI node (phase 1)
- Server: CSI ingest, recording, replay and analysis (phases 2, 4, 5)

Release type: **minor** (v0.0.0 → v0.1.0)

