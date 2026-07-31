# CSI node firmware

ESP-IDF 5.x, ESP32-S3. Three roles from one image, selected in `menuconfig`. The default is
`STATION`: one board, joined to the WiFi you already have.

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

4. **Binary wire format, never JSON.** At 100 Hz and ~158 bytes a frame this is ~16 KB/s; on-chip
   JSON encoding would fall over. (`csi_wire.h`)

5. **UDP, not WebSocket.** No reconnect logic on the device, and a dropped frame is survivable —
   it becomes a sequence gap, which the server measures. (`csi_net.c`)

6. **Power save off.** With it on the radio sleeps between beacons and a steady 80 Hz stream
   becomes bursts. Burst-sampled data is useless for spectral analysis no matter how good the
   timestamps are. (`csi_wifi.c`, `sdkconfig.defaults`)

7. **The probe is paced against an absolute deadline.** `vTaskDelayUntil`, not `vTaskDelay`, so
   a slow send costs one late probe rather than shifting every subsequent one. Rate stability is
   the only reason the prober exists. (`csi_probe.c`)

8. **Every association is counted and travels in the frame.** A mesh network can move the node
   to a different access point without breaking anything the sequence numbers or the device
   clock would notice. `link_epoch` is what lets the server notice. (`csi_wifi.c`, `csi_wire.h`)

## Topology

One board, station mode, joined to the access point you already have:

```
   [AP] ---- ICMP echo reply, 100 Hz ----> [node]  CSI callback on each reply
     ^                                        |
     +------- ICMP echo request --------------+
                                              |
                            same association ---> server:5566/udp
```

The node provokes its own downlink stream. It pings the gateway at a fixed rate and reports CSI
for each reply, so the link it measures is AP→node — the packets are transmitted by the access
point, and what modulates them is whatever is between the AP and the board.

That is the whole trick, and it is why one board is enough. A station that only listens gets a
CSI callback whenever the AP happens to address it, which on an idle network is a few frames a
second; the spectrum you compute from that is a spectrum of the household's traffic pattern.

**What you give up compared to the pair.** The rate is a request, not a fact. If the channel is
busy or the router rate-limits ICMP, some probes go unanswered and the stream has holes in it.
The statistics log reports the yield, the server refuses to estimate a breathing rate across a
hole rather than interpolating one into existence, and the node health view shows both. If the
yield turns out to be persistently poor, that is when a second board earns its place.

### On a mesh network

Several access points share one SSID, and the mesh decides which one you are on. It can change
its mind at any time.

That matters more here than it would for a normal client. A roam moves the far end of the link
to a different room: the presence detector's 30 s calibration, the baseline it drifts, and the
subcarrier ranking are all now describing geometry that does not exist, and nothing in the
amplitudes says anything happened.

So:

1. Boot once with `CSI_LOCK_BSSID` empty and read the scan log. It lists every access point on
   your SSID with its channel and RSSI — which the phone apps for Eero, Nest and Deco do not
   show you at all.
2. Pick one and set `CSI_LOCK_BSSID` to it. This is a real decision, not a formality: the line
   between that AP and the node is the line the system senses along, so the useful AP is the one
   that puts a doorway, a bed, or a hallway on that line. Not necessarily the strongest.
3. Flash, and watch `roams` on the node health view. It should stay at zero. A steering attempt
   against a locked station becomes a brief disconnect and a reconnect to the same AP — visible
   and counted, rather than silent.

Two more things these networks will do to you:

- **Guest SSIDs and client isolation** stop the node reaching the server at all. Put it on the
  main network.
- **WPA2/WPA3 transitional with PMF** is the common default. The firmware advertises PMF-capable
  for this reason; without it some systems refuse the join in a way that looks exactly like a
  wrong password.

Band steering needs no thought — the ESP32-S3 is 2.4 GHz only, so there is nothing to steer it
to.

### The ESP32 pair, if you add a second board

`RECEIVER` + `TRANSMITTER` is the plan's topology B. The transmitter emits a datagram at a fixed
rate and the receiver listens promiscuously, filtered to the transmitter's MAC, so the measured
link is TX→RX through the room and the rate is entirely yours. Set `CSI_PEER_MAC` on the
receiver to the transmitter's MAC, which the transmitter prints at boot.

## Build

```sh
. $IDF_PATH/export.sh
cd firmware

idf.py set-target esp32s3
idf.py menuconfig      # CSI node -> role, SSID, server address, BSSID lock
idf.py build flash monitor
```

### Settings that matter

| Setting | Default | Why |
|---|---|---|
| `CSI_LOCK_BSSID` | empty | The mesh setting. Empty means the network may move the node between access points mid-session, which invalidates every baseline without saying so |
| `CSI_NODE_ID` | 1 | Distinct per board, or the server interleaves two rooms into one history |
| `CSI_SERVER_HOST` | — | An address, not a hostname; DNS is one more thing to fail unattended |
| `CSI_PROBE_RATE_HZ` | 100 | Target 80–100 Hz; well above Nyquist for the ~2 Hz cardiac band. ~6 KB/s each way, continuously |
| `CSI_PROBE_METHOD` | ICMP | Ping the gateway. Switch to UDP echo if the yield is poor and the reply counter says the router is not answering |
| `CSI_PEER_MAC` | empty | Receiver role only. Empty captures from every station on the channel — fine for a smoke test, wrong after that |
| `CONFIG_FREERTOS_HZ` | 1000 | At the 10 ms default the probe period rounds and the rate is quietly wrong |

If you pick `CSI_PROBE_UDP_ECHO`, the server needs `CSI_ECHO_PORT` set to match — it does not
open that port otherwise.

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
I (30245) csi: link f0:9f:c2:11:22:33 epoch 1 | probes 1000 | replies 998 | yield 80% | probe errors 0 | disconnects 0
```

Against Phase 1's exit criteria:

- **rate** should sit within a percent or two of `CSI_PROBE_RATE_HZ`
- **ring drop** is frames the callback could not place because the sender fell behind. Non-zero
  here means something is blocking on Core 1 — check lwIP's affinity first
- **queued** creeping upwards is the same problem, earlier
- **filtered** counts frames from other stations; it should be large and is not a fault
- **send errors** are usually the network, not the node

The station role's second line is about the link rather than the board:

- **yield** is CSI frames over probes, and it is the headline number for a single node. Under
  100% means the access point did not answer every probe. Some of that is unavoidable on a
  shared channel; a yield that collapses is worth chasing.
- **replies** separates the two ways yield can fall. Replies arriving *without* CSI frames means
  the capture configuration is wrong, not the network. Replies not arriving means the router is
  not answering — try `CSI_PROBE_UDP_ECHO`, which the server answers unconditionally.
- **epoch** must not move. Each increment is a re-association, and on a mesh that means you may
  now be measuring a different room. If it climbs, the BSSID lock is not set or is not holding.
- **disconnects** climbing with a stable epoch is the lock doing its job — the network tried to
  steer the node and it came back to the same access point.

The `sequence gaps under 1%` criterion is measured at the server, on the Node health view, which
counts device-side drops and in-flight losses together — which is what you actually care about.

## Deferred

HT40 for 128 subcarriers is a `htltf_en` change here plus nothing at all downstream; `n_sub` is
per-frame and the server's layout tables are keyed on it. Same for a Nexmon node at 256.
