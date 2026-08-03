#!/usr/bin/env bash
#
# One command to get WiFi CSI sensing running on a Raspberry Pi:
#
#     curl -fsSL https://raw.githubusercontent.com/Saavuori/wifi-csi-app/main/install.sh | bash
#
# This sets up a working sensor, measuring your actual room. Two halves: the server in Docker,
# and — on a Pi whose radio nexmon_csi can patch (3B+, 4, 5, CM4) — this same Pi as a real CSI
# node against your access point. See pi/README.md. The firmware build takes 20-40 minutes and,
# on a Pi 5, one reboot; rerunning the same command resumes where it left off.
#
# On hardware nexmon cannot patch, the node step is skipped and you get the server alone, ready
# for ESP32 nodes to report to.
#
#     --no-node          server only; do not touch the Wi-Fi firmware
#     --node-id N        node id for this Pi, 1..254 (default 20)
#     --data-dir PATH    where recordings go (default ~/csi-data)
#     --http-port N      port for the web app (default 8080)
#     --udp-port N       port the nodes send to (default 5566)
#     --image REF        container image to run
#     --build            build the image from source instead of pulling it
#     --uninstall        stop and remove everything this script started
#     --yes              assume yes for prompts (needed when there is no terminal)
#
set -euo pipefail

IMAGE="${CSI_IMAGE:-ghcr.io/saavuori/wifi-csi-app:latest}"
REPO_URL="https://github.com/Saavuori/wifi-csi-app.git"
CONTAINER="csi"
# Only ever removed, never started. Earlier versions of this script ran a synthetic node here,
# so a re-run or an uninstall has to clean one up rather than leave it feeding fabricated
# frames into a server the user believes is measuring their room.
LEGACY_SYNTH_CONTAINER="csi-synth"
NETWORK="csi-net"

HTTP_PORT="${CSI_HTTP_PORT:-8080}"
UDP_PORT="${CSI_UDP_PORT:-5566}"
DATA_DIR="${CSI_DATA_DIR:-$HOME/csi-data}"

BUILD=0
UNINSTALL=0
ASSUME_YES=0
DOCKER="docker"
# On by default: a Pi that can sense, senses. Hardware nexmon cannot patch skips it on its own,
# and --no-node is there for a capable Pi that is deliberately only the server.
NODE=1
NODE_ID="${CSI_NODE_ID:-20}"
SRC_DIR=""

# 4 MB, matching the receive buffer the ingest socket asks for. See the note in ensure_sysctl.
WANT_RMEM=8388608

bold=""; dim=""; red=""; green=""; yellow=""; reset=""
if [ -t 1 ]; then
    bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'
    yellow=$'\033[33m'; reset=$'\033[0m'
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$bold" "$reset" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$reset" "$*"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$reset" "$*"; }
die()  { printf '%serror:%s %s\n' "$red" "$reset" "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Set up WiFi CSI sensing on a Raspberry Pi.

  --no-node          server only; do not touch the Wi-Fi firmware
  --node-id N        node id for this Pi, 1..254 (default 20)
  --data-dir PATH    where recordings go (default ~/csi-data)
  --http-port N      port for the web app (default 8080)
  --udp-port N       port the nodes send to (default 5566)
  --image REF        container image to run
  --build            build the image from source instead of pulling it
  --uninstall        stop and remove everything this script started
  --yes              assume yes for prompts (needed when there is no terminal)
EOF
}

# Reads from the terminal rather than stdin, because stdin is the script itself when this is
# piped from curl. With no terminal at all, fall back to the default answer.
ask() {
    local prompt="$1" default="${2:-n}" hint="[y/N]" reply
    if [ "$ASSUME_YES" = 1 ]; then return 0; fi
    [ "$default" = "y" ] && hint="[Y/n]"
    if [ ! -r /dev/tty ]; then
        say "  $prompt $hint (no terminal, assuming $default)"
        [ "$default" = "y" ]
        return
    fi
    printf '  %s %s ' "$prompt" "$hint" > /dev/tty
    read -r reply < /dev/tty || reply=""
    case "${reply:-$default}" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --node)      NODE=1 ;;
        --no-node)   NODE=0 ;;
        --node-id)   NODE_ID="${2:?--node-id needs a number}"; shift ;;
        --build)     BUILD=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --yes|-y)    ASSUME_YES=1 ;;
        --data-dir)  DATA_DIR="${2:?--data-dir needs a path}"; shift ;;
        --http-port) HTTP_PORT="${2:?--http-port needs a number}"; shift ;;
        --udp-port)  UDP_PORT="${2:?--udp-port needs a number}"; shift ;;
        --image)     IMAGE="${2:?--image needs a reference}"; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

SUDO=""
if [ "$(id -u)" != 0 ]; then
    command -v sudo > /dev/null 2>&1 || die "not root and sudo is not installed"
    SUDO="sudo"
fi

# ---------------------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------------------

check_arch() {
    case "$(uname -m)" in
        aarch64|arm64|x86_64|amd64) ;;
        armv6l|armv7l|armhf)
            die "this is a 32-bit OS ($(uname -m)). numpy and scipy publish no wheels for it,
       so the image has nothing to install. Reflash with 64-bit Raspberry Pi OS
       (Raspberry Pi Imager -> Raspberry Pi OS (64-bit)) and run this again."
            ;;
        *)  warn "unrecognized architecture $(uname -m); continuing anyway" ;;
    esac
}

install_docker() {
    step "Installing Docker"
    if ! ask "Install Docker from get.docker.com? This runs their script as root." y; then
        die "Docker is required. Install it and run this again."
    fi
    command -v curl > /dev/null 2>&1 || die "curl is needed to install Docker"
    curl -fsSL https://get.docker.com | $SUDO sh
    if [ -n "$SUDO" ]; then
        local me
        me="$(id -un)"
        $SUDO usermod -aG docker "$me" || true
        warn "added $me to the docker group; log out and back in for that to apply"
    fi
    ok "Docker installed"
}

ensure_docker() {
    step "Checking Docker"
    command -v docker > /dev/null 2>&1 || install_docker

    if docker info > /dev/null 2>&1; then
        DOCKER="docker"
    elif $SUDO docker info > /dev/null 2>&1; then
        # Group membership from a fresh `usermod` does not apply to the shell that ran it.
        DOCKER="$SUDO docker"
    else
        die "docker is installed but not running. Try: $SUDO systemctl enable --now docker"
    fi
    ok "$($DOCKER --version)"
}

ensure_sysctl() {
    step "Checking the UDP receive buffer"
    # The ingest socket asks the kernel for a 4 MB receive buffer, but Linux silently clamps
    # SO_RCVBUF to net.core.rmem_max — the default of ~208 KB is about one second of two nodes
    # at 80 Hz. Frames past that are dropped inside the kernel, where the server cannot see or
    # count them, and the symptom is unexplained sequence gaps under load.
    # Read /proc directly: sysctl(8) lives in /usr/sbin, which is not always on a regular
    # user's PATH on Debian.
    local current
    current="$(cat /proc/sys/net/core/rmem_max 2>/dev/null || echo 0)"
    case "$current" in ''|*[!0-9]*) current=0 ;; esac
    if [ "$current" -ge "$WANT_RMEM" ]; then
        ok "net.core.rmem_max is $current"
        return
    fi
    say "  net.core.rmem_max is $current, which will silently truncate the ingest buffer."
    if ask "Raise it to $WANT_RMEM and persist it in /etc/sysctl.d/90-csi.conf?" y; then
        printf 'net.core.rmem_max=%s\n' "$WANT_RMEM" | $SUDO tee /etc/sysctl.d/90-csi.conf > /dev/null
        $SUDO sysctl -q -w "net.core.rmem_max=$WANT_RMEM"
        ok "raised to $WANT_RMEM"
    else
        warn "left at $current — expect packet loss with more than one node"
    fi
}

check_storage() {
    local src
    src="$(findmnt -no SOURCE --target "$DATA_DIR" 2>/dev/null || echo "")"
    case "$src" in
        /dev/mmcblk*)
            warn "$DATA_DIR is on the SD card. Recording writes ~1 GB per day per node, which
    fills a card and wears it out. For anything longer than a test, put it on a
    USB SSD: rerun with --data-dir /mnt/ssd/csi"
            ;;
    esac
}

# ---------------------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------------------

# Puts a checkout of this repo in SRC_DIR, cloning one if we are not already in it. Both
# building the image and installing the node need the source; piped from curl there is none.
# Sets a global rather than echoing the path, so that it can log as it goes and so the clone
# is genuinely done once — a subshell's assignment would not survive to the second caller.
ensure_source() {
    [ -n "$SRC_DIR" ] && return 0

    if [ -f "$PWD/deploy/Containerfile" ] && [ -f "$PWD/pi/csi_node.py" ]; then
        SRC_DIR="$PWD"
        return 0
    fi

    step "Fetching the source"
    SRC_DIR="${TMPDIR:-/tmp}/wifi-csi-app"
    command -v git > /dev/null 2>&1 || die "git is needed to fetch the source"
    rm -rf "$SRC_DIR"
    git clone --depth 1 "$REPO_URL" "$SRC_DIR" > /dev/null 2>&1 \
        || die "could not clone $REPO_URL"
    ok "cloned into $SRC_DIR"
}

build_image() {
    ensure_source
    step "Building the image from source"
    say "  this takes a few minutes on a Pi"
    $DOCKER build -f "$SRC_DIR/deploy/Containerfile" -t "$IMAGE" "$SRC_DIR"
    ok "built $IMAGE"
}

ensure_image() {
    if [ "$BUILD" = 1 ]; then
        build_image
        return
    fi
    step "Pulling $IMAGE"
    if $DOCKER pull "$IMAGE"; then
        ok "pulled"
        return
    fi
    warn "could not pull the image — it may not be published yet, or the package may be private"
    if ask "Build it from source instead?" y; then
        build_image
    else
        die "no image to run"
    fi
}

# ---------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------

remove_container() {
    $DOCKER rm -f "$1" > /dev/null 2>&1 || true
}

start_server() {
    step "Starting the server"
    mkdir -p "$DATA_DIR"
    check_storage

    $DOCKER network create "$NETWORK" > /dev/null 2>&1 || true
    remove_container "$CONTAINER"
    # Upgrading from a version that ran one. Left in place it would keep feeding fabricated
    # frames in alongside the real node's, which is worse than no data at all.
    remove_container "$LEGACY_SYNTH_CONTAINER"

    # Recording on: everything that reaches this server is measured, and losing a session you
    # cannot repeat costs more than the disk does.
    $DOCKER run -d \
        --name "$CONTAINER" \
        --network "$NETWORK" \
        --restart unless-stopped \
        -p "${HTTP_PORT}:8080" \
        -p "${UDP_PORT}:5566/udp" \
        -v "$DATA_DIR:/data" \
        -e "CSI_RECORD=true" \
        "$IMAGE" > /dev/null
    ok "container $CONTAINER is up (recording)"
}

# ---------------------------------------------------------------------------------------
# This Pi as a sensor
# ---------------------------------------------------------------------------------------

# nexmon_csi patches the BCM43455, which is the radio on the 3B+, 4, 5 and CM4. Everything
# else Raspberry Pi has shipped carries a chip it cannot patch, so there is no point offering.
csi_capable_radio() {
    local model
    model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "")"
    case "$model" in
        *"Raspberry Pi 5"*|*"Raspberry Pi 4"*|*"Raspberry Pi 3 Model B Plus"*|*"Compute Module 4"*)
            return 0 ;;
        *)  return 1 ;;
    esac
}

install_node() {
    [ "$NODE" = 0 ] && return 0

    if ! csi_capable_radio; then
        warn "this board's Wi-Fi chip is not one nexmon_csi supports, so this Pi cannot sense
    for itself. The server is up and ready for ESP32 nodes to report to."
        return 0
    fi

    say ""
    step "Making this Pi a sensor"
    say "  Patching the Wi-Fi firmware with nexmon_csi: CSI against your access point with no"
    say "  ESP32 boards involved. ${dim}20-40 minutes to build, one reboot on a Pi 5.${reset}"
    say "  ${dim}Reversible with --uninstall. Pass --no-node for the server alone.${reset}"

    ensure_source
    [ -f "$SRC_DIR/pi/install-node.sh" ] \
        || { warn "pi/install-node.sh is missing; skipping"; return 0; }

    local args=(
        --server 127.0.0.1 --port "$UDP_PORT" --http-port "$HTTP_PORT" --node-id "$NODE_ID"
    )
    [ "$ASSUME_YES" = 1 ] && args+=(--yes)

    # The node talks to the server over the loopback: both are on this Pi, and the container
    # publishes the UDP port on the host.
    $SUDO bash "$SRC_DIR/pi/install-node.sh" "${args[@]}" \
        || warn "the node install did not finish; see the output above"
}

wait_healthy() {
    step "Waiting for the server"
    if ! command -v curl > /dev/null 2>&1; then
        warn "curl is not installed; skipping the health check"
        return 0
    fi
    local i
    for i in $(seq 1 60); do
        if curl -fsS "http://127.0.0.1:${HTTP_PORT}/api/healthz" > /dev/null 2>&1; then
            ok "healthy"
            return 0
        fi
        sleep 1
    done
    warn "no response after 60s. Logs: $DOCKER logs $CONTAINER"
    return 1
}

uninstall() {
    step "Removing"
    remove_container "$LEGACY_SYNTH_CONTAINER"
    remove_container "$CONTAINER"
    $DOCKER network rm "$NETWORK" > /dev/null 2>&1 || true
    ok "containers and network removed"

    if [ -f /etc/systemd/system/csi-node.service ]; then
        if [ -x /opt/csi-node/uninstall.sh ]; then
            $SUDO /opt/csi-node/uninstall.sh
        else
            $SUDO systemctl disable --now csi-node > /dev/null 2>&1 || true
            $SUDO rm -f /etc/systemd/system/csi-node.service /etc/default/csi-node
            $SUDO systemctl daemon-reload
            # Installed through update-alternatives, so the stock firmware is still on disk.
            $SUDO update-alternatives --auto brcmfmac43455-sdio.bin > /dev/null 2>&1 || true
            ok "csi-node service removed; reboot to load the stock Wi-Fi firmware"
        fi
    fi
    say ""
    say "Recordings were left alone. Delete them with:"
    say "  rm -rf $DATA_DIR"
}

summary() {
    local ip
    ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | head -n1 || echo "")"
    say ""
    say "${bold}Open the app:${reset}"
    say "  http://localhost:${HTTP_PORT}"
    [ -n "$ip" ] && say "  http://${ip}:${HTTP_PORT}   ${dim}(from another machine on this network)${reset}"
    say ""
    if systemctl is-active --quiet csi-node 2>/dev/null; then
        say "${bold}This Pi is also sensing.${reset} It reports as node $NODE_ID."
        say "  journalctl -u csi-node -f     ${dim}(rate, subcarriers, RSSI every 10 s)${reset}"
        say ""
    fi
    say "Point any further nodes at ${ip:-this host}:${UDP_PORT} and watch the Node health view."
    say ""
    say "${bold}Useful:${reset}"
    say "  $DOCKER logs -f $CONTAINER"
    say "  curl -s localhost:${HTTP_PORT}/api/status"
    say "  $DOCKER rm -f $CONTAINER   ${dim}(remove everything)${reset}"
    say ""
    warn "the web app has no authentication, and the UDP port accepts frames from anyone who"
    say "    can reach it. That is fine on a home LAN and not fine on the open internet."
}

main() {
    say "${bold}WiFi CSI sensing${reset} — Raspberry Pi setup"
    say ""

    if [ "$UNINSTALL" = 1 ]; then
        ensure_docker
        uninstall
        exit 0
    fi

    check_arch
    ensure_docker
    ensure_sysctl
    ensure_image
    start_server
    wait_healthy || true
    # After the server, so that the node has somewhere to send its first frame.
    install_node
    summary
}

main "$@"
