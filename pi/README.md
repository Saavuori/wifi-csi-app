# Raspberry Pi CSI node

A Pi with a patched Wi-Fi firmware, listening to one access point and forwarding what it hears
in the same uplink format the ESP32 nodes use. Nothing downstream knows the difference — the
pipeline is source-agnostic and `n_sub` is per frame, so a Pi and an ESP32 can sit side by side.

| | |
|---|---|
| Hardware | Pi 3B+, 4, 5 or CM4 — anything with the BCM43455 — **plus a USB Wi-Fi dongle** |
| Capture radio | onboard `wlan0`, monitor mode, never associated |
| Network radio | the dongle, `wlan1` — the association, the route in, and the probes |
| Subcarriers | 64 (20 MHz on the control channel); 128 / 256 if you capture wider |
| Firmware | [nexmon_csi](https://github.com/seemoo-lab/nexmon_csi), via [our fork](https://github.com/Saavuori/nexmon_csi) |
| Runs as | `csi-node.service`, plus `csi-ap-agent.service` for the picker |

A Pi Zero, Zero 2 W or original 3B carries a different chip and cannot do this at all.

## The one thing to understand: it takes two radios

The onboard radio captures in **monitor mode** and holds no association. That is not a
preference, and it is the reverse of what this directory used to claim.

While the extractor is armed, the shipped ucode deafens the receiver for the whole duration of
every CSI dump. Upstream considers this intended — the patch is written for monitor mode, and a
non-monitor receiver would need a different one. The consequence is that an associated radio
cannot carry traffic while it is collecting. Measured, in associated mode:

- DHCP never completes
- pings never arrive — inbound unicast is exactly what the deaf window eats
- the CSI that does come back trickles in at **~17 Hz**

So the association, the default route and your SSH session live on a **USB dongle**, and the
onboard radio does nothing but listen. This is what makes everything else safe: the two radios
are independent, and the capture radio can be retuned to any channel at any moment without the
dongle noticing. On a machine whose only route in is that dongle, that is the whole ballgame.

`install-node.sh` writes `/etc/NetworkManager/conf.d/99-csi-node.conf` to keep NetworkManager
off the capture interface permanently. Managed, it would take the interface back out of monitor
mode or try to associate it, and either one ends the capture with no error anywhere — the node
just reports zero frames.

## Installing

The top-level `install.sh` offers this automatically on supported hardware:

```bash
curl -fsSL https://raw.githubusercontent.com/Saavuori/wifi-csi-app/main/install.sh | bash
```

Answer yes when it asks about setting the Pi up as a sensor, or pass `--node` to skip the
prompt. To install only the node, pointing at a server elsewhere:

```bash
sudo pi/install-node.sh --server 192.168.1.10 --probe-iface wlan1
```

**Have the dongle in and connected first.** The installer checks for it and asks before
continuing without one, because the step that arms the extractor is the step that takes the
onboard radio off the network.

Two other things to expect. The firmware build takes **20-40 minutes** — it compiles a cross
toolchain before it compiles anything else. And on a **Pi 5 it needs one reboot** part way
through, because Raspberry Pi OS boots a 16 KB page kernel there and nexmon needs 4 KB. The
installer records the stage it reached, so running the same command again after the reboot
continues rather than starting over.

Undo it with `sudo pi/install-node.sh --uninstall`, which restores the stock firmware through
`update-alternatives` and hands the capture interface back to NetworkManager. Reboot afterwards.

### Why our fork of nexmon_csi

Upstream's procedure kills `wpa_supplicant` and expects you to build from a directory that has
CSI addresses reversed for it. [The fork](https://github.com/Saavuori/nexmon_csi) adds the
Raspberry Pi 5 and current-Raspberry-Pi-OS install path (`Makefile.rpi`, firmware installed as
a Debian alternative so it is reversible), makes `nexutil` failures detectable instead of
silently exiting 0, refuses to build from a firmware directory with no CSI port rather than
producing an image with no extractor in it, and ships `utils/csi-connected.sh`.

It also stopped claiming that the association keeps working while collection is armed. That
claim is the one this directory was built on, and it is wrong; the section above is the
measurement that settles it. `csi-connected.sh` survives here only as the disarm path
(`--stop`, which writes `csi_collect=0` and re-enables scanning).

## What it measures

Only 7.45.189 has reversed CSI addresses for the BCM43455, so that is the firmware version the
build patches regardless of what Raspberry Pi OS ships — the patched image replaces the
distribution's.

**One transmitter.** `capture-up.sh` passes `-m <bssid>` to `makecsiparams`, and on this build
the firmware honours it. That matters for more than tidiness: every dump costs a receive window,
so dumping on unrelated traffic costs frames we wanted. Measured on channel 9, adding the filter
took the node from 43 Hz to 52 Hz and foreign frames from ~45 Hz to zero. `csi_node.py` applies
the same filter in software from `/run/csi-node.env`, so the two always agree — if they
disagreed, every frame would be discarded and the node would report zero.

**One kind of frame.** A transmitter's frames are not interchangeable samples of one channel.
Block acks, QoS data and beacons are sent at different rates, widths and antenna weightings, and
each measures a visibly different channel. Measured on this link:

| | share of captured frames |
|---|---|
| block acks (`fctl` 0x94) | 98.2% |
| QoS data (0x88) | 1.8% |

Their medians correlate only **0.918** with each other, while each kind self-correlates above
**0.99**. Interleaved, the rare kind puts one stray row into the waterfall every time it
appears — about four a second, which reads as motion in a still room. Locking onto a single
`(frame_control, n_sub)` class cut waterfall spikes from **5.19% of frames to 1.68%**. With
`CSI_FCTL=auto` the node spends its opening couple of seconds counting and keeps the majority;
nothing is emitted meanwhile, because the server calibrates its baseline on the first frames it
sees and a baseline built from a mixture describes no channel.

**20 MHz on the control channel**, even when the access point runs HT40. Not a simplification:
`makecsiparams` takes 2.4 GHz 40 MHz only as `<control>u`, which encodes the block *below* the
control channel — measured, `9u` produces chanspec 0x1907, centred on channel 7 — while this AP's
HT40 sits above it (control 9, center1 2462 MHz = channel 11). The matching `9l` is accepted and
silently degrades to a 20 MHz chanspec. So the only 40 MHz capture available here would spend
half its bandwidth on spectrum the transmitter is not using. 20 MHz on the control channel is
what any 20 MHz receiver sees of those frames, and it yields 64 subcarriers — the exact layout
the ESP32 nodes produce and the DSP was tuned on.

### The rate is the household's, not ours

The node pings a host so that the measured radio has something to transmit, the same shape as
the station firmware's `CSI_PROBE_UDP_ECHO`. It is not, however, what sets the rate. In monitor
mode the extractor dumps every frame the chosen transmitter sends to *anyone*, so what arrives
is the whole household's traffic to that access point:

| | rate at the node |
|---|---|
| no probing at all | 201 Hz |
| probing at 200 Hz | 215 Hz |

Two hundred probes a second buy fourteen frames, because one block ack covers a whole aggregated
burst. The raw rate therefore follows whatever else is on the network and wanders between roughly
100 and 300 Hz.

That is why `CSI_MAX_HZ` exists. Breathing lives below 0.5 Hz and motion well under 10, so none
of that rate is needed; capping below the quiet-hours floor spends the surplus on regularity
instead. The spacing becomes near-uniform, which is what the server's resampler wants, and the
reported rate stops reporting the neighbours. The limiter runs against a deadline rather than
against a gap since the last frame kept — a gap systematically undershoots, because arrivals are
random and each late frame's lateness is inherited by the next. Measured on the Pi, that
difference was 85 Hz against a 120 Hz setting.

The probes still buy a floor. A mesh unit nobody is using sends beacons and little else, about
10 Hz, and pinging a host behind it forces every reply onto the air.

## Choosing which radio to measure

**In a mesh the nearest access point is usually the worst sensor.** What a CSI node measures is
the path between one transmitter and whatever it is transmitting to. The useful question is not
"which radio is strongest" but "which radio's traffic crosses the space I care about". A unit in
the same room as the Pi, with nothing moving between them, has a beautiful and perfectly static
channel.

```bash
csi-select-ap --list
csi-select-ap 7A:DA:88:A3:03:4B --probe 192.168.0.121   # far unit, driven via one of its clients
csi-select-ap --auto                                     # back to whatever the dongle uses
csi-select-ap --status
```

The same picker is in the web UI. Either way it moves the capture radio only: `wlan0` can
monitor any channel while `wlan1` stays associated wherever it likes, so **selecting an access
point can never cost you the machine**. That independence is the reason the picker is allowed to
exist on a headless box.

Two things follow from a choice:

- the capture radio moves to that BSSID's channel, and `CSI_AP_FOLLOW` becomes `manual` so the
  dispatcher stops re-deriving it when the dongle roams;
- **something has to make that radio transmit.** If it is the one the dongle is associated to,
  pinging the gateway does it. If it is another unit, `--probe` a host associated to *that* unit:
  replies come from whichever radio the host is on, and replies from any other radio are dropped
  by the transmitter filter. Get this wrong and you measure that AP's beacons, about 10 Hz, with
  nothing to say so except a low rate.

### How the web UI reaches the host

The server runs in a container; `nmcli`, `systemctl` and the nexmon tools are all on the host.
They talk through the data directory that is already bind-mounted (`~/csi-data` → `/data`), and
nothing in it grants the container any privilege — it writes a request naming a BSSID, and
`csi-ap-agent.service` decides what to do about it.

| File in the data directory | Written by | Contents |
|---|---|---|
| `aps.json` | the agent | what is in range, what is being measured, the probe settings |
| `ap-select.request.json` | the server | `{id, mode, bssid, probe_host}` |
| `ap-select.result.json` | the agent | `{id, ok, error, finished_at}` |

Both writers write a `.tmp` and rename, because the other side may read at any moment and half a
JSON document is worse than a stale one. The agent republishes `aps.json` on a timer and after
every selection.

## What nexmon does not give us

A nexmon packet from this build is **18 bytes** of header and then interleaved int16 CSI:

```
magic u16 | rssi i8 | fctl u8 | src_mac[6] | seq u16 | core/spatial u16 | chanspec u16 | chip u16
```

This file used to say 16, with `src_mac` read from offset 2, and that rejected every real frame.
A bcm43455c0 at 80 MHz emits 1042 bytes; against a 16-byte header that leaves 1026, and
`1026 % 4 == 2` fails the "an int16 pair per subcarrier" check. Nothing said so, because the
malformed-packet branch skipped past the status line that would have counted it. The node now
reports malformed packets from that branch too — a silent node is indistinguishable from a dead
radio.

`src_mac` and `rssi` arrive for free. The rest of what the uplink format needs is synthesized
here, and each is worth knowing about because each is a place where a wrong answer would look
like data rather than like a fault.

**Sequence numbers are minted, not copied.** nexmon reports the *802.11* sequence number of the
frame that triggered the capture. That is 12 bits, it wraps every 4096 frames, and it belongs to
the transmitter. Forwarded as-is it would wrap every ~41 s at 100 Hz, and each wrap would read as
a reboot. The node keeps its own monotonic counter.

**Timestamps come from the kernel, not the radio.** There is no radio-level timestamp to have.
The node uses `SO_TIMESTAMPNS`, stamped in the driver rather than in the forwarding process, so
it does not carry our scheduling jitter — but it is still further from the antenna than the
ESP32's in-callback `esp_timer_get_time()`. The breathing estimator resamples onto these
timestamps and cannot repair one that is wrong, so this is the most likely source of a quietly
worse answer than the ESP32 gives. The control message's width follows userspace, so a 32-bit
Raspberry Pi OS delivers an 8-byte `timespec32`; demanding 16 there sends every frame through the
wall-clock fallback, which looks fine and is not.

**RSSI comes from the frame.** This build carries a per-frame RSSI, one signed byte between the
magic and the MAC — and it is the only one available, because in monitor mode the interface is
not associated, so `/proc/net/wireless` does not list it and the driver's estimate would be a
confident zero. `LinkStats` stays as the fallback for a build that leaves the byte empty. Either
way the server only uses RSSI through a 30-second EMA, so per-frame precision was never the
point.

**`link_epoch` is inferred from the packets.** nexmon has no notion of an association, but it
reports the transmitter and the chanspec of every frame, and a change in either is exactly what
the epoch exists to signal — the server answers it by dropping the node's history, which is
correct, because every baseline built on the old link measures a different room. The clock and
the sequence counter stay monotonic across it, so the server records a roam rather than a roam
*and* a reboot.

**Amplitude is scaled per frame.** nexmon gives int16 per component; the wire carries int8. The
node scales each frame so its largest component lands near full scale. Absolute gain is
discarded, which matters less than it sounds — the server divides by the frame's own RMS in both
of its default normalization modes anyway, and int8 is exactly what the ESP32 delivers, so the
DSP was tuned on it. The real cost is that `preprocess.py`'s AGC step detector will not fire on
Pi frames: per-frame scaling removes the step before the server sees it. The step is gone rather
than flagged.

## Checking it works

```bash
journalctl -u csi-node -f
```

A status line every 10 seconds gives the rate, subcarrier count, RSSI, link changes, and how
many frames were dropped for being malformed, from another transmitter, of another class, of
another width, or over the rate limit. Those counters are the diagnosis: a node reporting
0 frames and a large "other classes" is a lock that outlived its class, not a dead radio.

Then open the app's Node health view, which measures the same things from the device timestamps:
a steady rate and sub-1% loss means the capture chain is good.

If the service is up but no frames arrive, work down this list:

- `csi-select-ap --status` — what is it actually measuring, and is the service running?
- `iw dev wlan0 info` — type should be `monitor`. If NetworkManager took it back, check that
  `/etc/NetworkManager/conf.d/99-csi-node.conf` is there and reload NetworkManager.
- `sudo tcpdump -i wlan0 dst port 5500` — is nexmon emitting at all? If this is silent the
  firmware side is the problem, not the forwarder. `nexutil -Iwlan0 -g501` reads the collect
  flag straight out of firmware shared memory; `capture-up.sh` checks it on every start, because
  `nexutil` reports transport failures on stderr and still exits 0.
- Is the probe host associated to the radio being measured? If it is not, its replies come from
  a different radio and are filtered out, and you are looking at beacons.
- `iw dev wlan1 link` — is the dongle still up? A roam re-derives the capture through the
  NetworkManager dispatcher, but only in `auto` mode.

## Configuration

`/etc/default/csi-node`, then `systemctl restart csi-node`. `csi-select-ap` rewrites several of
these in place, so hand edits to those can be overwritten.

| Variable | Default | Notes |
|---|---|---|
| `CSI_SERVER_HOST` | `127.0.0.1` | Where frames go |
| `CSI_UDP_PORT` | 5566 | |
| `CSI_NODE_ID` | 20 | 1..254, distinct per node |
| `CSI_IFACE` | `wlan0` | The capture radio. Monitor mode; do not put your network on it |
| `CSI_CHANSPEC` | derived | e.g. `9/20`. Written by `capture-up.sh` in auto mode |
| `CSI_AP_FOLLOW` | `auto` | `auto` follows the dongle; `manual` means a radio was chosen |
| `CSI_AP_MAC` | derived | The transmitter to measure |
| `CSI_PROBE_IFACE` | `wlan1` | The dongle: association, route in, probes. Never the capture radio |
| `CSI_PROBE_HOST` | the gateway | Must be associated to the radio being measured |
| `CSI_PROBE_HZ` | 100 | 0 to measure only what the network already carries |
| `CSI_PROBE_AUDIT_INTERVAL` | 300 | Seconds between probe audits, 0 to disable. An audit stops probing for a window and compares the rates, so the log says what the probes are actually buying rather than assuming |
| `CSI_PROBE_AUDIT_WINDOW` | 5 | Seconds each half of an audit lasts. A gap in supply, not a new epoch |
| `CSI_MAX_HZ` | 100 | Cap on the emitted rate, 0 for none. Buys regular spacing |
| `CSI_FCTL` | `auto` | Keep one frame class, e.g. `0x94`. `auto` learns the majority |
| `CSI_DATA_DIR` | `~/csi-data` | The directory the server container mounts as `/data` |
| `CSI_CONNECT_SH` | in the nexmon tree | `csi-connected.sh`, used only to disarm on stop |

`csi_node.py` takes the same settings as flags; run it with `--help`. It can be run by hand
against a Pi that is already collecting, which is the fastest way to try settings without
restarting the service — `run-node.sh` is what the unit uses, and it layers `/run/csi-node.env`
over the static config so the software filter always matches the firmware's.

## What is installed where

| Path | |
|---|---|
| `/opt/csi-node/bin/csi_node.py` | the forwarder |
| `/opt/csi-node/bin/capture-up.sh` | arms the extractor for the current choice, every start |
| `/opt/csi-node/bin/capture-down.sh` | disarms it on stop |
| `/opt/csi-node/bin/run-node.sh` | starts the forwarder with the derived environment |
| `/opt/csi-node/bin/csi-ap-agent.sh` | the picker's host half, run by `csi-ap-agent.service` |
| `/usr/local/sbin/csi-select-ap` | the picker, by hand |
| `/etc/NetworkManager/dispatcher.d/90-csi-node` | re-derives the capture when the dongle roams |
| `/etc/NetworkManager/conf.d/99-csi-node.conf` | keeps NetworkManager off the capture radio |
| `/run/csi-node.env` | what `capture-up.sh` derived: the live BSSID and chanspec |

## Tests

```bash
python -m pytest pi/tests -q
```

No Pi required: the tests build synthetic nexmon packets, push them through the node, and parse
the result with the *server's* protocol module. That last part is the point — the node holds a
fourth copy of the uplink header, alongside `firmware/main/csi_wire.h`, `server/csi/protocol.py`
and `web/src/lib/protocol.ts`, and this is what stops it drifting from the other three.

The header layout is asserted against the real 18 bytes and against a real 1042-byte 80 MHz
frame, because the previous tests built the same wrong header the parser expected and so passed
happily while the node rejected every frame on the actual hardware.
