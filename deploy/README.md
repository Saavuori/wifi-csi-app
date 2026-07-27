# Deployment

Alongside the existing apps: a podman container behind Caddy.

## Build and run

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
