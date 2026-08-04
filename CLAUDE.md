# CLAUDE.md

WiFi CSI sensing: one node (ESP32 or Raspberry Pi) captures Channel State Information, streams it
to a Python server over UDP, which records, replays, analyses and serves it to a canvas web app.

## Freshness

| | |
|---|---|
| **Written against** | `v0.3.0` — commit `42c076a` — 2026-08-04 |

Check whether this file has fallen behind before trusting it:

```bash
git log --oneline 42c076a..HEAD -- server/csi web/src pi/csi_node.py firmware/main docs deploy .github
```

No output means current. Any output is the list of commits that landed after this file was
written — read them, then use the tripwire table below to decide which sections to re-verify.
When you update this file, bump the three values above to the commit you verified against.

**Tripwires** — a change to the left invalidates the sections on the right:

| Changed | Re-verify |
|---|---|
| `server/csi/protocol.py`, `firmware/main/csi_wire.h`, `pi/csi_node.py`, `web/src/lib/protocol.ts` | Wire formats, Invariant 1 |
| `server/csi/hub.py` | Data path, Invariants 3–6 |
| `server/csi/dsp/*` | DSP layer, Invariant 7 |
| `server/csi/api.py` | HTTP + WebSocket surface |
| `web/src/main.ts`, `web/src/views/*` | Views |
| `server/pyproject.toml`, `web/package.json`, `check.sh`, `.github/workflows/*` | Commands, Releases |

## Layout

| Path | What |
|---|---|
| `server/csi/` | UDP ingest, echo responder, recorder, replayer, DSP, HTTP + WebSocket. FastAPI + uvicorn, numpy/scipy. Python ≥ 3.11. |
| `server/csi/dsp/` | Pure numpy/scipy, no I/O — preprocess, presence, selection, vitals, zones, subcarriers, util. |
| `web/src/` | TypeScript + canvas. **No framework**, no runtime deps. Vite. |
| `pi/csi_node.py` | Raspberry Pi node: nexmon_csi capture → uplink datagrams. ~1700 lines, reports as node **20**. |
| `firmware/main/` | ESP-IDF C for ESP32-S3/ESP32. Three roles: STATION, RECEIVER, TRANSMITTER. |
| `docs/wire-format.md` | Authoritative wire format spec. |
| `deploy/` | Containerfile, compose, reverse proxy. Image: `ghcr.io/saavuori/wifi-csi-app`. |
| `install.sh` | One-command Pi install (nexmon build + Docker server). `--uninstall` reverses it. |

## The data path

Everything enters through **one function**: `Hub.handle_datagram` (`server/csi/hub.py`), live from
UDP and replayed from disk alike.

```
parse → health.observe → record (live only) → preprocess → ring.push → fan out to clients
```

Analysis does **not** run here. A separate timer at `CSI_METRICS_HZ` (5 Hz) walks the per-node
rings, copies a window out via `history_for`, and runs the DSP in a worker thread
(`asyncio.to_thread`) — presence, zones, breathing, heart, placement SNR, subcarrier ranking.

Ports: UDP **5566** ingest, HTTP **8080**, `CSI_ECHO_PORT` off unless set (nexmon itself
broadcasts on 5500 host-side). Node rate ~80–100 Hz; default in-memory history 120 s per node.

## Invariants

These are the things that are cheap to break and expensive to notice. Most are argued at length
in the source — the code comments here explain *why*, and are worth reading before changing.

1. **The uplink header is mirrored in four places.** `server/csi/protocol.py`,
   `firmware/main/csi_wire.h`, `pi/csi_node.py`, and (downlink counterpart)
   `web/src/lib/protocol.ts`. Change one, change all four. `server/tests/test_protocol.py` pins
   the layout; `pi/tests/` parse the node's own datagrams with the server's module, which is what
   keeps the two Python copies in step.
2. **v2 appends to v1, never rearranges.** Both versions still parse, and that is not politeness:
   every recording ever made is a stream of datagrams in the version current when captured, and
   replay hands those exact bytes to the same parser. Dropping v1 retires the archive.
3. **Live and replay stay on one path.** This is what makes a recording a valid substitute for
   the room, which is what lets the detector be developed at a desk. Do not add a replay-only
   branch through preprocessing or analysis.
4. **The DSP worker never touches the live ring.** `history_for` copies on the event loop and is
   the boundary; a worker slicing the live ring eventually gets a window torn across the write
   pointer. Detector mutations that land mid-analysis are deferred via `NodeState._pending`.
5. **Metrics compute even with no browser connected.** The presence detector is stateful — 30 s
   calibration, continuously drifting baseline. Gating it on a connected client means opening a
   tab calibrates on you walking to the laptop, and the detector never fires again.
6. **A subcarrier mask change clears history**, exactly like a reboot or a roam. Rows written
   under the old mask carry NaN where the new mask says "valid", and nothing downstream survives
   that.
7. **Zone `window_s` and `band` are not runtime-patchable.** They belong to the feature version:
   changing either makes live features incomparable with stored fingerprints, and the failure is
   silent — every zone simply stops matching. Code change plus rebuild, not a slider.
8. **A client typo must not crash the server.** Everything in `Hub.update_config` goes through
   `_number`, which rejects (never clamps) out-of-range, non-finite, and `bool`. An uncaught raise
   here is a 500 over HTTP and a torn-down WebSocket over the socket.
9. **`server/csi/synth.py` is a test fixture, never a data source.** Nothing in the product
   imports it. A server showing fabricated frames a user believes are measurements fails in the
   direction of looking correct.

## DSP layer

Read the module docstrings — each states the design decision and the wrong approach it rejects.

- `preprocess.py` — masking, AGC step removal, Hampel impulse rejection. AGC steps otherwise
  dominate variance and read as someone walking through the room.
- `subcarriers.py` — which bins carry signal. Index `i` → subcarrier `k`: `k = i` for `i < n_sub/2`,
  else `i - n_sub`. Guards/DC/pilots are dropped.
- `selection.py` — magnitude gate first, then rank for the band. Ranking by raw variance picks
  deep fades (thermal noise); ranking by mean/variance picks carriers that never change.
- `presence.py` — windowed variance vs a calibrated empty-room baseline, hysteresis, debounce.
- `vitals.py` — PulseFi chain: bandpass → Savitzky-Golay → windowed FFT → peak. Breathing
  defaults to a **60 s** window (not the literature's 20 s) — measured spread on real Pi captures
  was 3.18 BPM at 20 s vs 0.15 at 60 s. Heart is the same code, different band, 5 s.
- `zones.py` — fingerprint classification, **not** localization. Nearest centroid on cosine
  distance over an L2-normalized per-subcarrier motion-band dB profile. `cross_validate` reports
  zones the recordings cannot separate rather than letting the classifier guess forever.
- `util.py` — `Coverage` travels with resampled windows: `np.interp` silently draws a straight
  line across a gap, and a ramp across 400 ms puts real power in the respiration band.

## Views

Ten, in `web/src/views/`: waterfall, subcarriers, motion, zones, breathing, heart, placement,
sessions, health, wifi. Breathing and heart share one **Vitals** nav entry. Each implements the
`View` interface (`views/view.ts`) — `mount()` subscribes, `unmount()` must release *every*
subscription and animation frame.

The WebSocket lives in a worker (`workers/socket.ts`), not the main thread: binary decode at
100 Hz never competes with rendering, and frames reach the main thread pre-batched. Text
WebSocket messages are JSON events at a few Hz; binary messages are CSI frames at node rate —
the client dispatches on `typeof ev.data`, no tag byte.

Layout is phone-first: the 320 px desktop control rail becomes a bottom sheet. This matters
because the placement tuner and zone recorder exist to be used while walking around the room.

## Commands

Everything checkable without hardware:

```bash
./check.sh
```

That runs server pytest, Pi node pytest, ruff on both, firmware host tests (plain `cc`, no
ESP-IDF), and the web typecheck + build. CI (`.github/workflows/ci.yml`) runs the same split
across four jobs plus a multi-arch image build.

Local server against live boards:

```bash
python -m venv .venv && .venv/bin/pip install -e "server[dev]" && (cd web && npm install && npm run build)
```

```bash
CSI_WEB_DIR=web/dist .venv/bin/python -m csi
```

Front-end work with hot reload (proxies `/api` and `/ws` to 8080):

```bash
cd web && npm run dev
```

Lint is ruff, line length **100**, rules `E,F,W,I,UP,B` (`B008` ignored). Web build is
`tsc --noEmit && vite build` — typecheck failures break the build.

## Conventions

- **Comments explain why, not what.** This codebase is unusually prose-heavy and deliberately so:
  most non-obvious lines carry the rationale, the failure mode observed, and often the rejected
  alternative. Match that density. A change that removes a "why" comment loses more than it saves.
- **Failures are named, not silent.** Rejected estimates carry a reason string to the UI; a blank
  panel looks like a broken server, a named rejection points at the actual problem.
- **Conventional commits.** `feat:`, `fix:` — lowercase, descriptive of the user-visible effect
  ("teach the room its zones, and say which one the movement is in").

## Releases

Version is decided by the merged PR: one `release:` label, or a conventional-commit title.
`major` / `minor` / `patch` / `skip` — see `CHANGELOG.md` for the table and
`.github/workflows/pr-release-type.yml` for the logic.

The **git tag is authoritative**. `.github/scripts/apply_version.py` writes it into
`server/pyproject.toml` and `web/package.json` and prepends the changelog entry. The running
server reports it on `/api/version`, in the `hello` snapshot, and in the header — resolved by
`server/csi/version.py` from build env → package metadata → `"dev"`, never an invented number.
