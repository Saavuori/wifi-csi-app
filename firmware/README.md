# CSI node firmware

ESP-IDF 5.x, ESP32-S3. Two roles from one image, selected in `menuconfig`.

## The rules this firmware exists to obey

These are the things that will quietly ruin a capture session if they are not right. Each one
has a comment at the code that implements it; they are collected here so the list is checkable.

1. **The CSI callback does nothing but `memcpy` and return.** It runs in the WiFi driver task on
   Core 0. The write into the ring is non-blocking with drop-on-full — no queue send with a
   timeout, no allocation, no logging. Blocking there is what causes dropped frames and watchdog
   trips. (`csi_capture.c`, `csi_ring.c`)

2. **The packer/sender task is pinned to Core 1, *and* lwIP is moved off Core 0.** Pinning alone
   is not enough: the TCP/IP task defaults to Core 0, so the actual `sendto` would still
   contend with the WiFi driver and you would have relocated the problem rather than fixed it.
   Both `xTaskCreatePinnedToCore(..., 1)` and `CONFIG_LWIP_TCPIP_TASK_AFFINITY_CPU1` are
   required. (`csi_net.c`, `sdkconfig.defaults`)

3. **Timestamps are taken on the device** with `esp_timer_get_time()` and travel in the packet.
   Server arrival time is never used for anything frequency-domain — the jitter would destroy
   the analysis. (`csi_capture.c`)

4. **Binary wire format, never JSON.** At 100 Hz and ~150 bytes a frame this is ~15 KB/s; on-chip
   JSON encoding would fall over. (`csi_wire.h`)

5. **UDP, not WebSocket.** No reconnect logic on the device, and a dropped frame is survivable —
   it becomes a sequence gap, which the server measures. (`csi_net.c`)

6. **Power save off.** With it on the radio sleeps between beacons and a steady 80 Hz stream
   becomes bursts. Burst-sampled data is useless for spectral analysis no matter how good the
   timestamps are. (`csi_wifi.c`, `sdkconfig.defaults`)

## Topology

The plan's option B, an ESP32 pair, is the default and the one to start with:

```
  [TX node] ---- 100 Hz UDP broadcast ----> (air)
       |                                      |
       +--- AP ---+                    [RX node] promiscuous, filters on TX's MAC,
                  |                        computes nothing, forwards CSI
                  |                             |
                  +--------- LAN ---------------+---> server:5566/udp
```

Both boards join the same access point, which is what gives the receiver a route to the server.
The receiver listens promiscuously and filters on the transmitter's MAC, so what it measures is
the direct TX→RX link through the room — not the access point's link, which would not move when
you move the node.

Option A (one node in station mode, harvesting CSI from router traffic) works too: flash a
single receiver and set `CSI_PEER_MAC` to your router's BSSID. You give up control of the
packet rate, which is why it is not the default.

## Build

```sh
. $IDF_PATH/export.sh
cd firmware

idf.py set-target esp32s3
idf.py menuconfig      # CSI node -> role, SSID, server address, peer MAC
idf.py build flash monitor
```

Configure the transmitter first, note its MAC from the boot log, then set that as
`CSI_PEER_MAC` on the receiver.

### Settings that matter

| Setting | Default | Why |
|---|---|---|
| `CSI_NODE_ID` | 1 | Distinct per board, or the server interleaves two rooms into one history |
| `CSI_SERVER_HOST` | — | An address, not a hostname; DNS is one more thing to fail unattended |
| `CSI_PEER_MAC` | empty | Empty captures from every station on the channel — fine for a smoke test, wrong after that |
| `CSI_TX_RATE_HZ` | 100 | Target 80–100 Hz; well above Nyquist for the ~2 Hz cardiac band |
| `CONFIG_FREERTOS_HZ` | 1000 | At the 10 ms default the transmit period rounds and the rate is quietly wrong |

## Host tests

The ring buffer and the wire header are plain C and are tested without hardware:

```sh
./scripts/run_host_tests.sh
```

This checks the byte offsets of the wire header against the spec, and hammers the ring from two
threads looking for torn frames — the failure a missing memory barrier produces, which on the
device would look like noise rather than like a bug.

## Reading the statistics log

Every 10 seconds:

```
I (30245) csi: 79.9 Hz | sent 799 | ring drop 0 | filtered 0 | oversize 0 | queued 0 | send errors 0 | heap 214532
```

Against Phase 1's exit criteria:

- **rate** should sit within a percent or two of `CSI_TX_RATE_HZ`
- **ring drop** is frames the callback could not place because the sender fell behind. Non-zero
  here means something is blocking on Core 1 — check lwIP's affinity first
- **queued** creeping upwards is the same problem, earlier
- **filtered** counts frames from other stations; it should be large and is not a fault
- **send errors** are usually the network, not the node

The `sequence gaps under 1%` criterion is measured at the server, on the Node health view, which
counts device-side drops and in-flight losses together — which is what you actually care about.

## Deferred

HT40 for 128 subcarriers is a `htltf_en` change here plus nothing at all downstream; `n_sub` is
per-frame and the server's layout tables are keyed on it. Same for a Nexmon node at 256.
