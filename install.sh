#!/usr/bin/env bash
#
# One command to get WiFi CSI sensing running on a Raspberry Pi:
#
#     curl -fsSL https://raw.githubusercontent.com/Saavuori/wifi-csi-app/main/install.sh | bash
#
# By default this also starts a synthetic node, so the whole stack — waterfall, presence,
# breathing — does something visible on a Pi with no ESP32 boards attached. Pass --no-demo
# when you have real hardware to point at it.
#
#     --no-demo          do not start the synthetic node
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
SYNTH_CONTAINER="csi-synth"
NETWORK="csi-net"

HTTP_PORT="${CSI_HTTP_PORT:-8080}"
UDP_PORT="${CSI_UDP_PORT:-5566}"
DATA_DIR="${CSI_DATA_DIR:-$HOME/csi-data}"

DEMO=1
BUILD=0
UNINSTALL=0
ASSUME_YES=0
DOCKER="docker"

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

  --no-demo          do not start the synthetic node
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
        --no-demo)   DEMO=0 ;;
        --demo)      DEMO=1 ;;
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
    USB SSD: $0 --data-dir /mnt/ssd/csi"
            ;;
    esac
}

# ---------------------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------------------

build_image() {
    step "Building the image from source"
    local src="$PWD"
    if [ ! -f "$src/deploy/Containerfile" ]; then
        src="${TMPDIR:-/tmp}/wifi-csi-app"
        command -v git > /dev/null 2>&1 || die "git is needed to build from source"
        rm -rf "$src"
        git clone --depth 1 "$REPO_URL" "$src"
    fi
    say "  this takes a few minutes on a Pi"
    $DOCKER build -f "$src/deploy/Containerfile" -t "$IMAGE" "$src"
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

    # With the synthetic node there is nothing to lose by not recording, and a test run has no
    # business filling up an SD card. With real hardware the app's own default applies:
    # recording is on, because losing a session you cannot repeat costs more than the disk.
    local record="true"
    if [ "$DEMO" = 1 ]; then record="false"; fi

    $DOCKER run -d \
        --name "$CONTAINER" \
        --network "$NETWORK" \
        --restart unless-stopped \
        -p "${HTTP_PORT}:8080" \
        -p "${UDP_PORT}:5566/udp" \
        -v "$DATA_DIR:/data" \
        -e "CSI_RECORD=$record" \
        "$IMAGE" > /dev/null
    ok "container $CONTAINER is up (recording: $record)"
}

start_synth() {
    step "Starting a synthetic node"
    remove_container "$SYNTH_CONTAINER"
    $DOCKER run -d \
        --name "$SYNTH_CONTAINER" \
        --network "$NETWORK" \
        --restart unless-stopped \
        "$IMAGE" \
        csi-synth --host "$CONTAINER" --nodes 2 --breathing 14 --scenario cycle > /dev/null
    ok "two synthetic nodes, 14 breaths/min, alternating empty and occupied"
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
    remove_container "$SYNTH_CONTAINER"
    remove_container "$CONTAINER"
    $DOCKER network rm "$NETWORK" > /dev/null 2>&1 || true
    ok "containers and network removed"
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
    if [ "$DEMO" = 1 ]; then
        say "The data is synthetic. Give the presence detector about a minute to finish"
        say "calibrating, then it tracks the scenario and the breathing view settles near 14."
        say ""
        say "${bold}When your boards arrive:${reset}"
        say "  $DOCKER rm -f $SYNTH_CONTAINER"
        say "  # then point CSI_SERVER_HOST at ${ip:-this host} — see firmware/README.md"
    else
        say "Point your nodes at ${ip:-this host}:${UDP_PORT} and watch the Node health view."
    fi
    say ""
    say "${bold}Useful:${reset}"
    say "  $DOCKER logs -f $CONTAINER"
    say "  curl -s localhost:${HTTP_PORT}/api/status"
    say "  $DOCKER rm -f $CONTAINER $SYNTH_CONTAINER   ${dim}(remove everything)${reset}"
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
    if [ "$DEMO" = 1 ]; then start_synth; fi
    wait_healthy || true
    summary
}

main "$@"
