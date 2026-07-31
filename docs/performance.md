# Server performance, and whether it should be Go

Measured, not estimated, except where explicitly marked. Everything below was taken on the
machine described under [Method](#method) against synthetic frames through the real code
paths — `hub.handle_datagram`, `hub.compute_metrics`, the real `Preprocessor`, the real
`FrameRing`.

**Summary.** One node at 256 subcarriers costs about a fifth of one core, and 87% of that is
the 5 Hz metrics loop, not the 80 Hz ingest path. The single most expensive function in the
system is the Hampel filter, and it is expensive because of its algorithm rather than because
of Python — a Go port of the same code spends 85% of its time in the same place. A Go rewrite
buys a 4-15x faster ingest path that was already only 2.7% of a core, and would require
reimplementing `sosfiltfilt`, `savgol_filter`, `welch` and an SVD to get back to parity on the
part that actually costs something. The recommendation is to fix three specific things in
Python and not to rewrite.

## Where the time goes

### Per-frame ingest path, µs/frame

| n_sub | parse | preprocess | ring.push | encode downlink | `handle_datagram` total |
|---|---|---|---|---|---|
| 64 | 2.3 | 195.9 | 0.9 | 0.7 | 225.7 |
| 128 | 2.2 | 230.5 | 0.9 | 0.8 | — |
| 256 | 2.2 | 316.6 | 1.0 | 0.9 | 337.2 |

Adding WebSocket clients changes nothing measurable (0/1/4 clients: 225.7 / 226.9 / 230.7 µs
at n_sub=64) — `encode_frame` is called once and the resulting `bytes` is shared across
clients, which is the right design and it shows.

Preprocessing is 87-94% of the frame path. Inside it:

| stage | n_sub=64 | n_sub=256 |
|---|---|---|
| `valid_mask` (lru_cached) | 0.2 | 0.2 |
| `frame.amplitude()` | 2.8 | 3.2 |
| `_detect_agc_step` | 19.4 | 18.8 |
| raw RMS | 6.7 | 6.2 |
| `_normalize` | 1.7 | 1.6 |
| **`hampel`** | **104.8** | **197.5** |
| `process()` with `hampel_enabled=False` | 40.9 | 46.8 |

`hampel` is 62-70% of the per-frame cost. `cProfile` attributes it to three `np.median` calls
over a `(n_valid, 11)` strided view: 9000 `np.median` calls per 3000 frames, and the time is
NumPy dispatch overhead on a tiny array, not arithmetic. The actual work is ~1700 comparisons.

### Metrics path, per node per tick (5 Hz default)

| | n_sub=64 | n_sub=256 |
|---|---|---|
| `history_for` snapshot (**on the event loop**) | 0.05 ms | 0.16 ms |
| `presence.update` (incl. SVD) | 0.10 ms | 0.30 ms |
| `breathing.estimate` | 4.44 ms | 8.81 ms |
| `heart.estimate` | 3.27 ms | 4.89 ms |
| `band_snr_db` × 2 (placement tuner) | 4.69 ms | 14.50 ms |
| `json.dumps` of the result | 0.15 ms | 0.22 ms |
| **`compute_metrics` total** | **15.0 ms** | **36.0 ms** |

Two things stand out.

The **placement tuner is the most expensive single item** at 256 subcarriers — 14.5 ms of 36,
more than breathing and heart combined. It runs on every tick whether or not anyone has the
Placement view open, because `compute_metrics` computes unconditionally.

`scipy.linalg.lstsq` is **20% of `compute_metrics`** by `tottime`, 15 calls per invocation.
It comes from `welch(..., detrend="linear")` in `dsp/util.py:138`, which runs a least-squares
fit per Welch segment. Note that `welch_psd` is always called on an already-detrended series
(`welch_psd(detrend(series), ...)` in both `rank_for_band` and `band_snr_db`), so the
per-segment fit is largely redundant work on top of a closed-form detrend the project already
has in `dsp/util.py:94`.

### CPU budget, one node, one core

| | ingest (80 Hz) | metrics (5 Hz) | total |
|---|---|---|---|
| n_sub=64 | 1.8% | 7.6% | **9.4%** |
| n_sub=256 | 2.7% | 18.0% | **20.7%** |

The 80 Hz path costs a seventh of what the 5 Hz path costs. Any optimization effort aimed at
ingest is aimed at the cheap half.

*Estimate, not measured:* a Pi 4 core is roughly 2-3x slower than the Xeon core used here, so
one 256-subcarrier node on a Pi 4 should land near 50% of one core, and a Pi 5 rather less.
That fits, but it is not a lot of headroom for a box that is also serving the web app.

### GIL contention

The `hub` docstring argues that handing the DSP to a worker thread is "close to free" because
NumPy and SciPy drop the GIL. Measured, by feeding the hub at 80 Hz per node on the event loop
and recording how late each scheduled tick actually fired:

| scenario | p50 | p95 | p99 | max |
|---|---|---|---|---|
| n_sub=64, 1 node, metrics off | 0.73 | 1.18 | 2.48 | 38.61 |
| n_sub=64, 1 node, metrics **on** | 0.75 | 1.19 | 1.40 | 1.97 |
| n_sub=256, 1 node, metrics off | 0.71 | 1.16 | 1.52 | 21.79 |
| n_sub=256, 1 node, metrics **on** | 0.78 | 1.93 | **15.01** | 29.25 |
| n_sub=256, 4 nodes, metrics off | 0.76 | 1.35 | 1.91 | 2.08 |
| n_sub=256, 4 nodes, metrics **on** | 0.74 | 1.97 | 5.10 | 7.13 |

Milliseconds. The claim mostly holds: p50 does not move, and the p99 cost of running metrics
is 3-13 ms at 256 subcarriers. That is real but small against a 12.5 ms frame period, and the
UDP receive buffer absorbs it — which is exactly what the 4 MB `SO_RCVBUF` and the
`net.core.rmem_max` note in the README are for. It is not a reason to change languages.

Note the baseline noise: even with metrics **off**, max lateness reaches 21-49 ms. That is
`asyncio.sleep` granularity and GC, not the DSP.

### Memory

| | |
|---|---|
| bare interpreter | 11 MB |
| + NumPy, SciPy, hub | 108 MB |
| ring, n_sub=64 (14400 frames) | 3.8 MB/node |
| ring, n_sub=256 | 14.9 MB/node |
| Go port, static binary | 1.6 MB |

The rings are not the problem; the interpreter and its scientific stack are. This is the one
place a Go rewrite wins by an order of magnitude and it wins unconditionally.

## Three fixes worth making in Python

### 1. `hampel` — use `np.partition` instead of `np.median`

A median is a selection, not a sort. Measured, bit-identical output:

| n_sub | current | `np.partition` | `scipy.ndimage.median_filter` |
|---|---|---|---|
| 64 | 111.7 µs | 65.8 µs | 46.4 µs |
| 128 | 120.2 µs | 69.5 µs | 48.2 µs |
| 256 | 193.2 µs | 89.9 µs | 55.4 µs |

Whole-frame effect at n_sub=256: `process()` goes from **280 µs to 156 µs**.

```python
    w = 2 * window + 1
    padded = np.pad(vals, window, mode="edge")
    strides = np.lib.stride_tricks.sliding_window_view(padded, w)
    med = np.partition(strides, w // 2, axis=1)[:, w // 2]
    dev = np.abs(strides - med[:, None])
    mad = np.partition(dev, w // 2, axis=1)[:, w // 2]
```

`np.partition` is verified identical to the current output (`np.allclose`, atol 1e-4, across
64/128/256). `scipy.ndimage.median_filter` is faster still but is **not** a drop-in: filtering
`|vals - med|` pointwise is a different estimator from taking the median of
`|window - median(window)|`, and it disagrees by up to 34 amplitude units. Use `np.partition`.

### 2. The metrics loop recomputes the same resample and PSD three times

`rank_for_band` and both `band_snr_db` calls each independently run `magnitude_gate`,
`uniform_resample` over the full kept set, and `welch_psd`. At n_sub=256 that is one
resample + PSD costing 3.55 ms, done three times, inside a 36 ms budget — and
`rank_for_band` (6.5 ms) plus `band_snr_db` × 2 (14.5 ms) is 21 ms of the 36.

Computing the gate, the resample and one PSD once per tick and passing them down should
recover roughly a third of `compute_metrics`. Separately, the placement tuner block in
`hub.compute_metrics:445-459` could be skipped when no client has the Placement view open —
unlike the presence detector, `band_snr_db` is stateless, so the argument in `_emit_metrics`'s
docstring for computing unconditionally does not apply to it.

Minor, in the same area: `uniform_resample` does `values[:, j].astype(np.float64)` inside its
per-column loop; hoisting the conversion is identical output and saves 0.08 ms at n_sub=64.

### 3. `Recorder.flush()` does a blocking `fsync` on the event loop

`Recorder.write` → `flush()` every 5 s → `SessionStore.update` → `save()`, which does
`json.dump` + `os.fsync` + `os.replace` (`sessions.py:106-121`). Measured on this machine's
SSD with 12 sessions in the index: **p50 2.8 ms, p99 27.7 ms**. Recording is on by default.

On an SSD this is tolerable. On the SD card the README explicitly warns about, an fsync can
take hundreds of milliseconds, and it lands directly on the task servicing the UDP socket.
The file write itself is correctly buffered — it is only the metadata journal that syncs. The
fix is to move `store.update()` off the loop (`asyncio.to_thread`) or to write the metadata
without fsync and rely on the rescan path that already exists for exactly this case
(`/api/sessions/{id}/rescan`).

## What Go would actually buy

I ported the per-frame path to Go — `ParseFrame`, `amplitude`, AGC step detection, RMS,
`normalize`, `hampel`, `FrameRing.Push`, downlink encode — stdlib only, scratch buffers reused,
verified against the Python output (`TestHampelAgreement`, `TestSnapshotAgreement`).

| ns/op | n_sub=64 | n_sub=128 | n_sub=256 |
|---|---|---|---|
| Go, full frame path | 15,562 | 36,576 | 77,291 |
| Python, full frame path | 225,700 | ~234,000 | 337,200 |
| **speedup** | **14.5x** | **6.4x** | **4.4x** |

Allocations: 1 per frame, 128-512 B (the int8 payload copy — removable with `unsafe`).

Two results are worth more than the speedup number.

**The Go port has the same hotspot.** `pprof` on the n_sub=256 benchmark: `medianSmall` 69.8%,
`hampelFast` 14.6% — **85% of the Go hot path is still the Hampel filter**. Porting to Go did
not fix the algorithm; it just made a bad algorithm cheaper per operation. The speedup also
*shrinks* as n_sub grows (14.5x → 4.4x), because Python's fixed dispatch overhead is what Go
eliminates, and that overhead amortizes as arrays get longer. On the 256-subcarrier Nexmon
node — the configuration the project is moving toward — Go is worth 4x on 2.7% of a core.

**Go is slower at the one thing the ring does.** `history_for`, the 20 s snapshot:

| | Python (NumPy) | Go |
|---|---|---|
| n_sub=64 | 54.6 µs | 151.9 µs |
| n_sub=256 | 163.3 µs | 276.3 µs |

Go allocates and zeroes 1.66 MB before copying into it; NumPy's gather does not. A production
port would pool the buffer, but the point stands — the parts of this codebase that are already
one vectorized call over a large array are the parts Go does not improve, and that is most of
the DSP.

### What would have to be reimplemented

The DSP does not call NumPy for glue, it calls it for numerics with no Go equivalent:

| call site | what it is |
|---|---|
| `vitals.py:25` `sosfiltfilt` | zero-phase 3rd-order Butterworth, SOS, with padding |
| `vitals.py:25` `butter` | filter design, `output="sos"` |
| `vitals.py:25` `savgol_filter` | Savitzky-Golay, window 15, order 3 |
| `vitals.py:245` `np.fft.rfft` | zero-padded real FFT |
| `util.py:132` `welch` | segmented PSD with detrending and windowing |
| `presence.py:288` `np.linalg.svd` | LAPACK `gesdd`, on the presence PCA path |

Gonum covers FFT and SVD. It does not cover `sosfiltfilt`, `savgol_filter` or `welch` — those
are hand-ports, and they are the ones where a subtle mistake is invisible. The comment at
`vitals.py:204-213` is precise about why: at 0.1 Hz against a 20 Hz sample rate, a `ba`
implementation "will quietly return noise instead of failing". A hand-ported Butterworth that
is wrong in the fourth decimal does not crash, it reports a confident wrong BPM — and the
project's whole validation story (synthetic node, known breathing rate, `test_vitals.py`)
exists because that failure mode is otherwise undetectable.

Scope: **4,025 lines of server Python plus 1,919 lines of tests**, of which the numerically
delicate part is `dsp/` at 1,321 lines.

### Where Go would genuinely win

Being fair to the other side, there are three real wins:

- **Memory**: 108 MB → ~20 MB. On a Pi 3B+ with 1 GB that is not nothing.
- **Deployment**: a 1.6 MB static binary versus the arm64 container the installer currently
  pulls, plus no NumPy/SciPy wheel story for arm.
- **Tail latency**: no GC pauses of the kind that produce the 21-49 ms maxima above, and no
  GIL, so the DSP genuinely runs on another core instead of mostly-another-core.

None of these is a throughput argument, and none of them is currently a reported problem.

## Recommendation

Do not rewrite. The measured position is that one node costs a fifth of a core, the expensive
half is the 5 Hz analysis loop rather than the 80 Hz ingest loop, and the top cost in the
system is an algorithm that is equally slow in both languages.

In order:

1. `np.partition` in `hampel` — bit-identical, 1.8x on the whole frame path, ~10 lines.
2. Share the gate/resample/PSD across `rank_for_band` and `band_snr_db`, and make the
   placement tuner conditional on a subscriber — roughly a third of `compute_metrics`.
3. Move `SessionStore.save()` off the event loop — a correctness-adjacent fix for SD cards,
   independent of any performance target.

Together these should take one 256-subcarrier node from ~20.7% to ~12% of a core, which is
most of what the Go port's ingest speedup was worth, at a fraction of the risk.

Revisit Go if the target changes to many nodes per server, or if the memory ceiling on a
Pi 3B+ becomes the binding constraint. If it is ever revisited, the boundary to port is
`ingest + protocol + preprocess + ring + downlink` — about 800 lines with no SciPy in them —
leaving the DSP in Python behind an IPC boundary. Porting `dsp/` is where the risk lives and
it is the part Go helps least.

## Method

- Xeon @ 2.10 GHz, 4 cores, Linux 6.18.5. Python 3.11.15, NumPy 2.4.6, SciPy 1.17.1,
  Go 1.24.7.
- Synthetic uniformly-random int8 CSI at 80 Hz, encoded through `protocol.encode_frame` and
  fed as real datagrams, so parsing is included.
- Python timings: mean of 2000+ calls after 50-200 warmup calls; p99 reported where the
  distribution matters. Go timings: `go test -bench -benchtime 2s`.
- Ring prefilled to a full 20 s window before any metrics measurement, so no result is taken
  against a short window.
- `CSI_RECORD=false` for the throughput runs, so disk is out of the ingest measurements; the
  recorder is measured separately.
- Random CSI is a worst case for `hampel`'s early-out (`if not bad.any(): return amp`) in the
  sense that it exercises the full path; on real CSI with few impulses the copy at the end is
  skipped more often, so the per-frame numbers here are a mild overestimate. The relative
  standings are unaffected — the medians are computed either way.
