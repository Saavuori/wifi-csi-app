# Wire formats

Two independent binary formats, plus one file container. All little-endian.

- **Uplink** — node → server, UDP. This is the format the plan calls "v1".
- **Downlink** — server → browser, WebSocket binary frames.
- **Recording** — length-prefixed uplink datagrams on disk.

A single canonical definition lives in four places that must stay in sync:

| Language | File | Pinned by |
|---|---|---|
| C (firmware) | `firmware/main/csi_wire.h` | `firmware/scripts/run_host_tests.sh` |
| Python (server) | `server/csi/protocol.py` | `server/tests/test_protocol.py` |
| TypeScript (browser) | `web/src/lib/protocol.ts` | `server/tests/test_protocol.py` |
| Python (Pi node) | `pi/csi_node.py` | `pi/tests/test_csi_node.py` |

`server/tests/test_protocol.py` pins the byte layout so drift is caught by CI rather than by a
silent parse failure at 100 Hz. The Pi node's copy is pinned differently and more strongly: its
tests parse the datagrams it produces with `server/csi/protocol.py` itself, so the two cannot
disagree about the layout without a test failing.

---

## Uplink: node → server (UDP)

```
offset size field        type   notes
0      2    magic        u16    0x4353. On the wire (LE) the bytes read 53 43.
2      1    version      u8     1
3      1    node_id      u8     1..254; 0 and 255 reserved
4      4    seq          u32    monotonic per node, wraps at 2^32
8      8    timestamp    u64    esp_timer_get_time(), microseconds since boot
16     1    rssi         i8     dBm, from wifi_pkt_rx_ctrl_t
17     1    noise_floor  i8     dBm
18     1    channel      u8     primary channel, 1..14
19     1    sec_channel  u8     0=none 1=above 2=below
20     2    n_sub        u16    number of subcarriers in this frame
22     ...  data         i8[]   2 * n_sub bytes, interleaved (imag, real)
```

Header is 22 bytes, `#pragma pack(1)`-equivalent — no padding anywhere. A HT20 frame with
64 subcarriers is `22 + 128 = 150` bytes, so at 100 Hz a node emits ~15 KB/s of payload.

`n_sub` is explicit and per-frame. Nothing downstream may assume 64. HT20 gives 64, HT40 gives
128, and a Raspberry Pi running Nexmon on an 80 MHz channel sends 256. The subcarrier layout
tables in `server/csi/dsp/subcarriers.py` are keyed on `n_sub`.

### Two kinds of producer

The header is written by an ESP32 (`firmware/`) or by a Pi running Nexmon (`pi/`). The fields
mean the same thing either way, but three of them are *synthesized* on the Pi rather than read
from the radio, and it is worth knowing which:

| Field | ESP32 | Pi |
|---|---|---|
| `timestamp` | `esp_timer_get_time()`, in the CSI callback | kernel receive timestamp (`SO_TIMESTAMPNS`) |
| `seq` | incremented per CSI callback | minted per forwarded frame; the 802.11 sequence number is *not* used |
| `rssi` | `wifi_pkt_rx_ctrl_t` | the driver's estimate from `/proc/net/wireless`, sampled at 1 Hz |
| `data` | `memcpy` of the driver's int8 buffer | int16 scaled per frame into int8 |

The Pi's `seq` is deliberately not the one Nexmon reports: that is the transmitter's 802.11
sequence number, 12 bits wide, and it wraps every 4096 frames. Forwarded as-is, a wrap reads
as a reboot roughly every 41 seconds at 100 Hz.

There is no `link_epoch` in v1, so the Pi signals a roam or an AP channel switch by restarting
both its clock and its counter — which is exactly how a node reboot presents, and the server
already answers a reboot by dropping the node's history. See `pi/README.md`.

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
