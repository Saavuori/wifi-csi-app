# Deployment

Two shapes. A Raspberry Pi on a home LAN, which is what most people want and what
`install.sh` does in one command. Or a podman container behind Caddy, alongside the existing
apps, which is what this server runs.

## Settings worth knowing

| Variable | Default | Why you might change it |
|---|---|---|
| `CSI_MAX_AGE_H` | 24 | How long a recording is kept. What a monitor left running is asked for is the recent past, so anything whose data is entirely older than this is deleted. Age is measured from the *end* of a recording: an overnight run is kept while any part of it is still inside the window. `0` disables it and leaves only the size cap. |
| `CSI_ROLL_H` | 1 | How often the always-on `live` recording is closed and a new one started. This is what lets `CSI_MAX_AGE_H` work at all — one endless session never finishes, so it can never age out. Hand-started captures never roll. `0` disables. |
| `CSI_MAX_DISK_GB` | 8 | Cap on the recordings directory, applied after the age rule as a backstop for a node writing faster than a day's worth was sized for. A node writes roughly a gigabyte a day and the default home for it is the SD card the OS is also on. `0` disables pruning, which means the appliance eventually fills the card it boots from. |
| `CSI_DROP_DC_ADJACENT` | false | Set to `true` for a Raspberry Pi node. The BCM43455 leaks its DC offset into the subcarriers either side of centre, measured at around eight times the median amplitude, and subcarrier selection ranks by variance — so the bin carrying no signal is a candidate to be chosen. An ESP32 does not have this and should leave it off. |
| `CSI_RECORD` | true | Recording everything costs disk; losing a session you cannot repeat costs more. |

## Raspberry Pi

```sh
curl -fsSL https://raw.githubusercontent.com/Saavuori/wifi-csi-app/main/install.sh | bash
```

Installs Docker if it is missing, raises `net.core.rmem_max`, pulls the arm64 image and starts the
server. On a Pi whose radio nexmon_csi can patch it also makes this Pi a sensor, measuring against
your access point; `--no-node` leaves the Wi-Fi firmware alone and gives you the server by itself.
`--uninstall` reverses all of it. `--help` lists the rest.

Requirements: **64-bit** Raspberry Pi OS on a Pi 4 or 5. On a 32-bit install the script stops
and tells you why: numpy and scipy publish no armv7 wheels, so there is nothing for pip to
install and a source build of scipy on a Pi is an afternoon.

Or with compose, from a clone:

```sh
docker compose -f deploy/compose.yaml up -d
```

Two host settings the container cannot do for itself:

- **`net.core.rmem_max`.** [`ingest.py`](../server/csi/ingest.py) asks for a 4 MB UDP receive
  buffer, but Linux clamps `SO_RCVBUF` to this sysctl *silently* — no error for the code to
  log. The ~208 KB default is about one second of two nodes at 80 Hz, and what you see when it
  overflows is sequence gaps with nothing in the logs to explain them.
  ```sh
  echo 'net.core.rmem_max=8388608' | sudo tee /etc/sysctl.d/90-csi.conf && sudo sysctl --system
  ```
- **Where `/data` lives.** A node at 80 Hz writes about 1 GB a day. Pointed at the SD card that
  is a wear problem as much as a capacity one; use a USB SSD for anything longer than a test,
  or run with `CSI_RECORD=false`.

The image is `ghcr.io/saavuori/wifi-csi-app:latest`, built for linux/amd64 and linux/arm64 by
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) after the tests pass.

## Build and run

`Containerfile` is a plain Dockerfile — `docker build -f deploy/Containerfile .` works
identically. Building for a Pi from an amd64 machine needs the platform stated, because a plain
build produces an amd64 image that will not start there:

```sh
docker buildx build --platform linux/arm64 -f deploy/Containerfile -t csi:latest .
```

The front-end stage is pinned to `$BUILDPLATFORM`, so that cross-build does not run `tsc` and
`vite` under emulation — its output is JavaScript, which has no architecture.

```sh
podman build -f deploy/Containerfile -t csi:latest .

podman volume create csi-data

podman run -d --name csi --restart=always \
  -p 127.0.0.1:8087:8080 \
  -p 5566:5566/udp \
  -v csi-data:/data \
  csi:latest
```

Two different bindings on purpose:

- **HTTP is bound to loopback.** Caddy terminates TLS and proxies to it; nothing else should
  reach 8080 directly.
- **UDP is bound to all interfaces.** The nodes send from the LAN, so this port has to be
  reachable from outside the host. It is the one place the container is genuinely exposed.

If the nodes are on a different network from the server, that UDP port needs a route. There is
no authentication on it — anything that can reach it can inject frames. On a home LAN that is
fine; over the open internet it is not, and the answer is a tunnel rather than adding a shared
secret to a device with no operator.

Only add `-e CSI_ECHO_PORT=5568 -p 5568:5568/udp` if a node is built with `CSI_PROBE_UDP_ECHO`
— i.e. its router will not answer pings and it needs something that will. It is off by default
because it reflects whatever it is sent, and an unnecessary reflector on the LAN is not free.

## Caddy

```caddyfile
csi.example.com {
    encode zstd gzip

    # The waterfall's WebSocket carries binary CSI frames continuously. The default proxy
    # timeouts will cut a long-lived socket; these keep an overnight session connected.
    reverse_proxy 127.0.0.1:8087 {
        flush_interval -1
        transport http {
            read_timeout 24h
            write_timeout 24h
        }
    }
}
```

`flush_interval -1` disables response buffering, which matters here: with buffering on, Caddy
holds frames until its buffer fills, and the waterfall arrives in visible jerks rather than
scrolling.

## Systemd

```sh
podman generate systemd --new --name csi --files
mkdir -p ~/.config/systemd/user
mv container-csi.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now container-csi
loginctl enable-linger "$USER"   # so it survives logout
```

## Disk

A node at 80 Hz produces about 12 MB/minute, so roughly 1 GB per day per node. `CSI_RECORD=true`
is the default because losing a session you cannot repeat costs more than the disk does — but
an unattended long-run deployment wants a retention policy.

Check what is there:

```sh
podman exec csi du -sh /data/recordings
podman exec csi ls -la /data/recordings
```

Sessions are deletable from the Sessions view, which removes the recording and its index
together. `sessions.json` is a plain file — safe to edit by hand when you mislabel a session at
one in the morning, which is the reason it is a file and not a database.

## Health

```sh
curl -s localhost:8087/api/healthz
curl -s localhost:8087/api/status | jq '.nodes, .counters'
```

`counters.bad_packets` climbing means something is sending to the UDP port that is not a CSI
node — a wrong `CSI_SERVER_PORT` somewhere, or a port scan. `counters.suppressed_live` climbing
means a replay is running and live frames are being ignored, which is intended.
