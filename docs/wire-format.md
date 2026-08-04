# Wire formats

Two independent binary formats, plus one file container. All little-endian.

- **Uplink** — node → server, UDP. Currently v2; v1 is still parsed.
- **Downlink** — server → browser, WebSocket binary frames.
- **Recording** — length-prefixed uplink datagrams on disk.

The uplink definition lives in three places that must stay in sync — one per kind of node, plus
the server that parses them all:

| Language | File | Pinned by |
|---|---|---|
| C (ESP32 firmware) | `firmware/main/csi_wire.h` | `firmware/scripts/run_host_tests.sh` |
| Python (server) | `server/csi/protocol.py` | `server/tests/test_protocol.py` |
| Python (Pi node) | `pi/csi_node.py` | `pi/tests/test_csi_node.py` |

The browser never sees an uplink datagram — it gets the downlink format below, defined in
`server/csi/downlink.py` and `web/src/lib/protocol.ts`.

`server/tests/test_protocol.py` pins the byte layout so drift is caught by CI rather than by a
silent parse failure at 100 Hz. The Pi node's copy is pinned differently and more strongly: its
tests parse the datagrams it produces with `server/csi/protocol.py` itself, so the two cannot
disagree about the layout without a test failing.

---

## Uplink: node → server (UDP)

```
offset size field        type   notes
0      2    magic        u16    0x4353. On the wire (LE) the bytes read 53 43.
2      1    version      u8     2
3      1    node_id      u8     1..254; 0 and 255 reserved
4      4    seq          u32    monotonic per node, wraps at 2^32
8      8    timestamp    u64    esp_timer_get_time(), microseconds since boot
16     1    rssi         i8     dBm, from wifi_pkt_rx_ctrl_t
17     1    noise_floor  i8     dBm
18     1    channel      u8     primary channel, 1..14
19     1    sec_channel  u8     0=none 1=above 2=below
20     2    n_sub        u16    number of subcarriers in this frame
--- v2 ends v1 here ---------------------------------------------------------
22     6    src_mac      u8[6]  who transmitted this frame: the AP's BSSID for a station node,
                                the peer node's MAC for the ESP32 pair
28     1    link_epoch   u8     increments on every association on the node
29     1    reserved     u8     zero
30     ...  data         i8[]   2 * n_sub bytes, interleaved (imag, real)
```

Header is 30 bytes, `#pragma pack(1)`-equivalent — no padding anywhere. A HT20 frame with
64 subcarriers is `30 + 128 = 158` bytes, so at 100 Hz a node emits ~16 KB/s of payload.

### Versions

v1 was the same header without the last eight bytes. v2 **appends**, so every v1 field is at the
offset it always was, and `parse_frame` handles both with one struct: a v1 datagram yields a zero
`src_mac` and epoch 0.

That is not politeness towards old firmware. The recorder stores raw datagrams and the replayer
hands those exact bytes to this parser, so a version the parser has forgotten is a shelf of
recordings that no longer open. Any future field goes on the end for the same reason.

### What link_epoch is for

A single node joined to a mesh network can be steered from one access point to another at any
time. The sequence numbers stay continuous and the device clock keeps running, so nothing else
in the frame changes — but the far end of the measured link has moved to a different room, and
every baseline built from the old one is now describing geometry that does not exist. The epoch
is the only thing that says so. `nodes.py` treats a change in it exactly as it treats a reboot:
throw the node's history away.

Setting `CSI_LOCK_BSSID` in the firmware prevents the roam in the first place; the epoch is what
tells you whether the lock is holding.

`n_sub` is explicit and per-frame. Nothing downstream may assume 64. HT20 gives 64, HT40 gives
128, and a Raspberry Pi running Nexmon on an 80 MHz channel sends 256. The subcarrier layout
tables in `server/csi/dsp/subcarriers.py` are keyed on `n_sub`.

### Two kinds of producer

The header is written by an ESP32 (`firmware/`) or by a Pi running Nexmon (`pi/`). Both are
stations probing an access point, so the fields mean the same thing either way — but on the Pi
several are *synthesized* rather than read from the radio, and it is worth knowing which:

| Field | ESP32 | Pi |
|---|---|---|
| `timestamp` | `esp_timer_get_time()`, in the CSI callback | kernel receive timestamp (`SO_TIMESTAMPNS`) |
| `seq` | incremented per CSI callback | minted per forwarded frame; the 802.11 sequence number is *not* used |
| `rssi` | `wifi_pkt_rx_ctrl_t` | the driver's estimate from `/proc/net/wireless`, sampled at 1 Hz |
| `noise_floor` | `wifi_pkt_rx_ctrl_t` | usually unavailable; sent as 0 |
| `src_mac` | the associated BSSID | read from the nexmon packet: whoever transmitted the frame |
| `link_epoch` | incremented on association | incremented when the transmitter or chanspec changes |
| `data` | `memcpy` of the driver's int8 buffer | int16 scaled per frame into int8 |

The Pi's `seq` is deliberately not the one Nexmon reports: that is the transmitter's 802.11
sequence number, 12 bits wide, and it wraps every 4096 frames. Forwarded as-is, a wrap reads
as a reboot roughly every 41 seconds at 100 Hz.

Nexmon has no notion of an epoch, but it reports the transmitter and the chanspec of every
frame, and a change in either is exactly what the epoch exists to signal — a roam, a reconnect,
or the access point changing channel. The Pi's clock and counter stay monotonic across one, so
the server sees a roam rather than a roam *and* a reboot. See `pi/README.md`.

The Pi's amplitude is scaled per frame, which discards absolute gain. That costs less than it
sounds — `preprocess.py` divides by the frame's own RMS in both default normalization modes —
but it does mean the AGC step detector cannot fire on a Pi frame, because the step was removed
before the server saw it.

### Why (imag, real) and not (real, imag)

That is the order `esp_wifi` hands to the CSI callback. The firmware does a straight `memcpy`
of the driver's buffer — no byte shuffling in the callback — so the wire inherits the layout.

### Sequence numbers

`seq` increments once per CSI callback that made it into the ring buffer, *not* once per
datagram sent. Frames dropped on the device (ring buffer full) therefore show up as a gap,
which is what we want: the gap statistic counts real losses, on-device and in-flight alike.

---

## Downlink: server → browser (WebSocket)

The socket carries two kinds of message:

- **Binary** messages are CSI frames, at the full node rate (decimated on request).
- **Text** messages are JSON events — metrics, presence state, node health, session control.
  These run at ≤10 Hz and JSON costs nothing at that rate.

Splitting on the WebSocket frame type means the client dispatches on `typeof ev.data` with no
tag byte and no ambiguity.

### Binary CSI message

```
offset size field      type   notes
0      2    magic      u16    0x4344
2      1    version    u8     1
3      1    node_id    u8
4      4    seq        u32    copied from the uplink frame
8      8    timestamp  u64    device µs, copied from the uplink frame
16     1    rssi       i8
17     1    noise_floor i8
18     1    channel    u8
19     1    flags      u8     bit0: AGC step detected on this frame
20     2    n_sub      u16
22     2    _pad       u16    zero — keeps `amp` 4-byte aligned
24     ...  amp        f32[n_sub]
```

Header is 24 bytes so `amp` lands on a 4-byte boundary and the browser can do
`new Float32Array(buf, 24, n_sub)` with no copy. That alignment is the entire reason for the
pad; do not remove it.

`amp` is amplitude **after** server-side preprocessing: RSSI normalization, Hampel filtering,
and masking. Subcarriers excluded by the mask (guards, DC, pilots) are `NaN` rather than
removed, so the array index is always the subcarrier index and the waterfall's Y axis stays
stable when the mask changes. Clients render `NaN` as a gap.

Preprocessing is server-side and identical for live and replayed frames — that is what makes a
recording a faithful stand-in for the room.

### History block (HTTP, not the socket)

`GET /api/nodes/{id}/history?seconds=N` returns the server's ring for one node as a single
binary block. A client that has just connected uses it to backfill its waterfall instead of
starting from an empty canvas and taking a screen-width of frames to show anything. `seconds`
is clamped to `CSI_HISTORY_S`; the response is `204` when the node has sent nothing yet.

```
offset          size  field      type   notes
0               2     magic      u16    0x4348
2               1     version    u8     1
3               1     node_id    u8
4               2     n_sub      u16
6               2     _pad       u16    zero
8               4     count      u32    columns in this block
12              4     _pad       u32    zero — keeps `t_us` 8-byte aligned
16              8n    t_us       f64[count]        device µs, oldest first
16+8n           4n·s  amp        f32[count][n_sub] row-major, one row per column
16+8n+4n·s      n     agc        u8[count]         1 where an AGC step was detected
```

Header is 16 bytes rather than the 12 its fields need so `t_us` lands 8-byte aligned for
`new Float64Array(buf, 16, count)`; `amp` then starts at `16 + 8·count`, which is 4-aligned for
free. Timestamps are float64 here rather than the u64 the per-frame format uses, so the browser
is not handed a `BigInt64Array` to unpack — exact to the microsecond for ~285 years of uptime.

Per column the content is the same as the binary CSI message above, including `NaN` at masked
subcarriers. Transposed into arrays because a client backfilling wants columns in bulk, not a
stream of frames to reassemble.

---

## Recording container

```
offset size field
0      8    magic       "CSIREC01"
8      ...  records
```

Each record is:

```
2    len   u16   length of the datagram that follows
len  data  u8[]  the uplink datagram, verbatim
```

The datagram is stored byte-for-byte as it arrived. Replay re-injects those bytes through the
same parser the UDP listener uses, which is what makes "replays byte-identically" a property
of the design rather than a thing to test for.

### Sidecar index

`<recording>.idx` holds fixed 16-byte entries written every `INDEX_STRIDE` (256) frames:

```
0  8  timestamp u64   device µs of the frame at this offset
8  8  offset    u64   byte offset into the .csi file
```

The index is an optimization, never a source of truth: it is rebuilt by scanning if missing or
truncated (a crash mid-write leaves a short index, not a corrupt one). Seeking to time T means
binary-searching the index for the last entry ≤ T, then scanning forward at most 256 frames.
