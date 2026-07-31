# Deployment

Two shapes. A Raspberry Pi on a home LAN, which is what most people want and what
`install.sh` does in one command. Or a podman container behind Caddy, alongside the existing
apps, which is what this server runs.

## Raspberry Pi

```sh
curl -fsSL https://raw.githubusercontent.com/Saavuori/wifi-csi-app/main/install.sh | bash
```

Installs Docker if it is missing, raises `net.core.rmem_max`, pulls the arm64 image, starts the
server, and — unless you pass `--no-demo` — starts a synthetic node so there is something to
look at without hardware. `--uninstall` reverses all of it. `--help` lists the rest.

Requirements: **64-bit** Raspberry Pi OS on a Pi 4 or 5. On a 32-bit install the script stops
and tells you why: numpy and scipy publish no armv7 wheels, so there is nothing for pip to
install and a source build of scipy on a Pi is an afternoon.

Or with compose, from a clone:

```sh
docker compose -f deploy/compose.yaml --profile demo up -d
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
