# Raspberry Pi CSI node

A Pi with a patched Wi-Fi firmware, measuring CSI against your access point and forwarding it
in the same uplink format the ESP32 nodes use. Nothing downstream knows the difference — the
pipeline is source-agnostic and `n_sub` is per frame, so 256 subcarriers at 80 MHz drop in
without a code change anywhere else.

| | |
|---|---|
| Hardware | Pi 3B+, 4, 5 or CM4 — anything with the BCM43455 |
| Subcarriers | 64 / 128 / 256, following the channel width the AP uses |
| Firmware | [nexmon_csi](https://github.com/seemoo-lab/nexmon_csi), via [our fork](https://github.com/Saavuori/nexmon_csi) |
| Runs as | `csi-node.service` |

A Pi Zero, Zero 2 W or original 3B carries a different chip and cannot do this at all.

## Installing

The top-level `install.sh` installs this alongside the server, with no flag needed — on hardware
nexmon_csi can patch, it is what the one-line install does:

```bash
curl -fsSL https://raw.githubusercontent.com/Saavuori/wifi-csi-app/main/install.sh | bash
```

`--no-node` opts out and leaves the Wi-Fi firmware alone. To install only the node, pointing at a
server elsewhere:

```bash
sudo pi/install-node.sh --server 192.168.1.10
```

Two things to expect. The firmware build takes **20-40 minutes** — it compiles a cross
toolchain before it compiles anything else. And on a **Pi 5 it needs one reboot** part way
through, because Raspberry Pi OS boots a 16 KB page kernel there and nexmon needs 4 KB. The
installer records the stage it reached, so running the same command again after the reboot
continues rather than starting over.

Undo it with `sudo pi/install-node.sh --uninstall`, which restores the stock firmware through
`update-alternatives`. Reboot afterwards to load it.

## Why our fork of nexmon_csi

Upstream's procedure gives up the Wi-Fi connection: it kills `wpa_supplicant`, switches to
monitor mode, and retunes the chip to whatever chanspec you passed. On a Pi that is meant to
be both the sensor and the thing forwarding frames to a server, losing the network is fatal.

[The fork](https://github.com/Saavuori/nexmon_csi/tree/claude/rpi-wifi-csi-capture-368a70)
adds `makecsiparams -k` (keep the current channel) and `-S` (keep scanning enabled), stops
`nexutil -s500` from suppressing scanning permanently, and ships `utils/csi-connected.sh`,
which reads the channel from the running association and configures the extractor around it.
Monitor mode turns out to be needed only to accept frames *not* addressed to the Pi, and we
do not need those.

The consequence is the main operating constraint below.

Only the CSI patch is ours. The base [nexmon](https://github.com/seemoo-lab/nexmon) tree —
the cross toolchain and the firmware patching framework — is built unmodified from upstream;
the fork is what lands in `patches/bcm43455c0/7_45_206/nexmon_csi`. The installer defaults to
the fork and pins the branch, and either side can be pointed elsewhere without editing it:

| | flag | environment | default |
|---|---|---|---|
| CSI patch | `--repo` | `CSI_NEXMON_REPO` | `Saavuori/nexmon_csi` |
| its branch | `--branch` | `CSI_NEXMON_BRANCH` | `claude/rpi-wifi-csi-capture-368a70` |
| base nexmon | `--nexmon-repo` | `CSI_NEXMON_BASE_REPO` | `seemoo-lab/nexmon` |

`fetch_sources` prints both before it builds, and a resumed install whose existing checkout
came from a different repository is repointed at the configured one rather than updated in
place.

## What it measures, and what you have to give it

While associated, the chip hands up CSI only for frames addressed to this Pi, plus broadcast.
With no traffic of your own, that means beacons — about 10 Hz. So the node pings the access
point at 100 Hz by default and measures the replies. This is the same problem the station
firmware solves with `CSI_PROBE_UDP_ECHO`, and the same answer: generate the traffic, measure
the reply.

10 Hz is not useless. Breathing sits at 0.1–0.5 Hz and is comfortably sampled there, so
`--probe-hz 0` is a real option if you would rather not add traffic. Heart rate is not.

A roam, or a channel switch by the AP, ends the capture. The node notices — the transmitter MAC
or the chanspec changes — and increments `link_epoch`, which the server answers by dropping the
node's history. That is the correct response: every baseline built on the old link is measuring
a different room. The clock and the sequence counter stay monotonic across it, so the server
records a roam rather than a roam *and* a reboot.

## What nexmon does not give us

A nexmon packet is 16 bytes of header and then interleaved int16 CSI. `src_mac` arrives for
free — it is in every packet. The rest of what the uplink format needs is synthesized here, and
each is worth knowing about because each is a place where a wrong answer would look like data
rather than like a fault.

**Sequence numbers are minted, not copied.** nexmon reports the *802.11* sequence number of
the frame that triggered the capture. That is 12 bits, it wraps every 4096 frames, and it
belongs to the transmitter rather than to us. Forwarded as-is it would wrap every ~41 s at
100 Hz, and each wrap would read as a reboot. The node keeps its own monotonic counter.

**Timestamps come from the kernel, not the radio.** There is no radio-level timestamp to have.
The node uses `SO_TIMESTAMPNS`, stamped in the driver rather than in the forwarding process,
so it does not carry our scheduling jitter — but it is still further from the antenna than the
ESP32's in-callback `esp_timer_get_time()`. The breathing estimator resamples onto these
timestamps and cannot repair one that is wrong, so this is the most likely source of a quietly
worse answer than the ESP32 gives.

**RSSI comes from the driver.** Upstream nexmon_csi carries no RSSI. Because we stay
associated we can read the driver's own estimate from `/proc/net/wireless`, once a second.
That sounds too slow to matter and is not: the server's hybrid normalization only uses RSSI
through a 30-second EMA, so a per-frame value would buy nothing.

**`link_epoch` is inferred from the packets.** nexmon has no notion of an association, but it
reports the transmitter and the chanspec of every frame, and a change in either is exactly what
the epoch exists to signal. That is the roam handling described above.

**Amplitude is scaled per frame.** nexmon gives int16 per component; the wire carries int8.
The node scales each frame so its largest component lands near full scale. Absolute gain is
discarded, which matters less than it sounds — the server divides by the frame's own RMS in
both of its default normalization modes anyway, and int8 is exactly what the ESP32 delivers,
so the DSP was tuned on it. The real cost is that `preprocess.py`'s AGC step detector will not
fire on Pi frames: per-frame scaling removes the step before the server sees it. The step is
gone rather than flagged.

## Known limitation: nexutil cannot reach the firmware

Verified on a Pi 4 Model B, Debian 13 (trixie), kernel 6.12, on 2026-08-01. Everything up to the
capture works: the firmware builds, installs and loads —
`brcmfmac: Firmware: BCM4345/6 wl0: version 7.45.189 (nexmon.org/csi: 54de-4)` — and `wlan0`
stays associated on channel 40 at 80 MHz, which is the 256-subcarrier configuration.

What does not work is configuring the extractor. `nexutil` has two transports and neither
reaches this driver:

| Build | Result |
|---|---|
| `-DUSE_NETLINK` (upstream default) | `nex_init_netlink: socket error (93: Protocol not supported)` |
| ioctl (no netlink) | `__nex_driver_io: error ret=-1 errno=95` (`EOPNOTSUPP`) |

Netlink needs nexmon's **patched brcmfmac module**, and the fork ships those only for kernels
4.19, 5.4 and 5.10. The ioctl path needs private ioctls the stock brcmfmac no longer accepts.
So `csi-connected.sh` configures nothing, and `tcpdump -i wlan0 dst port 5500` is silent.

The netlink build fails especially badly: its socket error goes to stderr but it still **exits
0**, so `csi-connected.sh`'s own error check passes and it prints "CSI collection enabled"
having done nothing. `install-node.sh` now builds the ioctl variant instead, which at least
fails visibly.

One further symptom worth knowing: with the patched firmware loaded, `wlan0` associates and
gets IPv6 by SLAAC but **never completes DHCP**. Transmission works — DHCP requests are visible
on the air — but no reply is received. A Pi reached only over Wi-Fi will disappear after the
reboot that loads this firmware. Have Ethernet or a console attached.

To undo:

```bash
sudo /opt/csi-node/uninstall.sh && sudo reboot
```

## Checking it works

```bash
journalctl -u csi-node -f
```

A status line every 10 seconds gives the rate, subcarrier count, RSSI and how many link
changes and malformed packets it has seen. Then open the app's Node health view, which
measures the same things from the device timestamps: a steady rate and sub-1% loss means the
capture chain is good.

If the service is up but no frames arrive, work down this list:

- `iw dev wlan0 link` — still associated? If not, the chanspec is wrong for this AP.
- `sudo tcpdump -i wlan0 dst port 5500` — is nexmon emitting at all? If this is silent the
  firmware side is the problem, not the forwarder.
- Is anything generating traffic? With `--probe-hz 0` and a quiet link there is genuinely
  nothing to measure.
- `iw dev wlan0 get power_save` — power save lets the chip doze between beacons, which shows
  up as gaps. `csi-connected.sh` turns it off, but NetworkManager will turn it back on;
  `nmcli connection modify <name> wifi.powersave 2` makes that stick.

## Configuration

`/etc/default/csi-node`, then `systemctl restart csi-node`.

| Variable | Default | Notes |
|---|---|---|
| `CSI_SERVER_HOST` | `127.0.0.1` | Where frames go |
| `CSI_UDP_PORT` | 5566 | |
| `CSI_NODE_ID` | 20 | 1..254, distinct per node |
| `CSI_IFACE` | `wlan0` | |
| `CSI_PROBE_HZ` | 100 | 0 to send nothing and ride the beacons |
| `CSI_AP_MAC` | the associated BSSID | Only measure frames from this transmitter |

`csi_node.py` takes the same settings as flags; run it with `--help`. It can be run by hand
against a Pi that is already configured for collection, which is the fastest way to try
settings without restarting the service.

## Tests

```bash
python -m pytest pi/tests -q
```

No Pi required: the tests build synthetic nexmon packets, push them through the node, and parse
the result with the *server's* protocol module. That last part is the point — the node holds a
third copy of the uplink header, alongside the ESP32's C and the server's Python, and this is
what stops it drifting from the other two.
