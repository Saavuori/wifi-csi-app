#!/usr/bin/env bash
#
# Turn this Raspberry Pi into a CSI node: patch the Wi-Fi firmware with nexmon_csi, install
# the forwarder, and run both from systemd.
#
# Normally invoked by the top-level install.sh via --node, but it stands alone:
#
#     sudo pi/install-node.sh --server 192.168.1.10
#
# The firmware build is long (20-40 minutes on a Pi) and, on a Pi 5, needs one reboot before
# it can start. The script is resumable: it records which stage it reached and picks up there,
# so running it again after the reboot continues rather than starting over.
#
#     --server HOST      where to send frames (default: this host)
#     --port N           server UDP port (default 5566)
#     --node-id N        node id, 1..254 (default 20)
#     --iface NAME       wireless interface (default wlan0)
#     --ap MAC           only measure frames from this transmitter (default: the associated AP)
#     --probe-hz N       ping rate that generates the traffic to measure (default 100)
#     --repo URL         nexmon_csi fork to build
#     --branch NAME      branch of that fork
#     --skip-build       assume the firmware is already patched; just install the service
#     --uninstall        remove the service and restore the stock firmware
#     --yes              assume yes for prompts
#
set -euo pipefail

NEXMON_REPO="https://github.com/seemoo-lab/nexmon.git"
CSI_REPO="${CSI_NEXMON_REPO:-https://github.com/Saavuori/nexmon_csi.git}"
CSI_BRANCH="${CSI_NEXMON_BRANCH:-claude/rpi-wifi-csi-capture-368a70}"

# nexmon patches one firmware version per chip directory, and this is the pairing that the
# Raspberry Pi's BCM43455 ships with.
CHIP="bcm43455c0"
FW_VERSION="7_45_206"

PREFIX="/opt/csi-node"
NEXMON_DIR="$PREFIX/nexmon"
STATE_DIR="/var/lib/csi-node"
STATE_FILE="$STATE_DIR/stage"
ENV_FILE="/etc/default/csi-node"
SERVICE="/etc/systemd/system/csi-node.service"

SERVER=""
PORT="${CSI_UDP_PORT:-5566}"
NODE_ID="${CSI_NODE_ID:-20}"
IFACE="${CSI_IFACE:-wlan0}"
AP_MAC=""
PROBE_HZ="${CSI_PROBE_HZ:-100}"
SKIP_BUILD=0
UNINSTALL=0
ASSUME_YES=0

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
        --server)     SERVER="${2:?--server needs a host}"; shift ;;
        --port)       PORT="${2:?--port needs a number}"; shift ;;
        --node-id)    NODE_ID="${2:?--node-id needs a number}"; shift ;;
        --iface)      IFACE="${2:?--iface needs a name}"; shift ;;
        --ap)         AP_MAC="${2:?--ap needs a MAC}"; shift ;;
        --probe-hz)   PROBE_HZ="${2:?--probe-hz needs a number}"; shift ;;
        --repo)       CSI_REPO="${2:?--repo needs a URL}"; shift ;;
        --branch)     CSI_BRANCH="${2:?--branch needs a name}"; shift ;;
        --skip-build) SKIP_BUILD=1 ;;
        --uninstall)  UNINSTALL=1 ;;
        --yes|-y)     ASSUME_YES=1 ;;
        -h|--help)    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || die "this needs root: sudo $0 $*"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------------------
# Stages. The firmware build survives a reboot; the reboot is what the Pi 5 needs before it
# can start. Recording the stage is what makes "run it again" the recovery procedure.
# ---------------------------------------------------------------------------------------

stage_done() {
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$1" > "$STATE_FILE"
}

stage_reached() {
    [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$1" ]
}

# ---------------------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------------------

pi_model() {
    tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown"
}

check_hardware() {
    step "Checking the radio"
    local model
    model="$(pi_model)"
    say "  $model"

    case "$model" in
        *"Raspberry Pi 5"*|*"Raspberry Pi 4"*|*"Raspberry Pi 3 Model B Plus"*|*"Compute Module 4"*)
            ok "this board carries the BCM43455, which nexmon_csi patches"
            ;;
        *"Raspberry Pi"*)
            die "this Pi's Wi-Fi chip is not one nexmon_csi supports. CSI capture needs the
       BCM43455, which means a Pi 3B+, 4, 5 or CM4. A Pi Zero, Zero 2 W or an
       original 3B will not work no matter what is installed."
            ;;
        *)
            warn "not a Raspberry Pi as far as /proc/device-tree/model says; continuing anyway"
            ;;
    esac

    [ -d "/sys/class/net/$IFACE" ] || die "no interface $IFACE (pass --iface)"
}

# Raspberry Pi OS on a Pi 5 boots a 16 KB page kernel by default. nexmon's firmware patching
# toolchain needs 4 KB pages; on the 16 KB kernel the build produces a firmware that does not
# load, which presents as Wi-Fi simply being gone after a reboot rather than as a build error.
check_pagesize() {
    local pagesize
    pagesize="$(getconf PAGESIZE 2>/dev/null || echo 4096)"
    if [ "$pagesize" = "4096" ]; then
        ok "4 KB pages"
        return 0
    fi

    step "Switching to the 4 KB page kernel"
    say "  This Pi boots a ${pagesize}-byte page kernel. nexmon needs 4096."
    local config="/boot/firmware/config.txt"
    [ -f "$config" ] || config="/boot/config.txt"
    [ -f "$config" ] || die "cannot find config.txt to edit"

    if grep -q '^kernel=kernel8.img' "$config"; then
        warn "$config already selects kernel8.img but the running kernel still has
    ${pagesize}-byte pages — you are running the old one. Reboot and run this again."
        exit 0
    fi

    if ! ask "Add 'kernel=kernel8.img' to $config and reboot?" y; then
        die "cannot continue on a ${pagesize}-byte page kernel"
    fi

    cp "$config" "$config.csi-backup"
    printf '\n# Added by csi-node: nexmon needs a 4 KB page kernel.\nkernel=kernel8.img\n' \
        >> "$config"
    stage_done "pagesize"
    ok "set; the old config is at $config.csi-backup"
    say ""
    say "${bold}Reboot, then run this again to continue.${reset}"
    say "  sudo reboot"
    exit 0
}

# ---------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------

install_deps() {
    step "Installing build dependencies"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # git/gcc/make build the toolchain; the rest are nexmon's own requirements. flex, bison
    # and libfl-dev build its C parser; texinfo and libtool build the cross-binutils.
    apt-get install -y -qq \
        git gcc make automake libtool texinfo flex bison libfl-dev \
        gawk qpdf tcpdump iw python3 python3-numpy \
        raspberrypi-kernel-headers > /dev/null 2>&1 || \
    apt-get install -y -qq \
        git gcc make automake libtool texinfo flex bison libfl-dev \
        gawk qpdf tcpdump iw python3 python3-numpy > /dev/null
    ok "installed"

    # nexmon's build scripts are Python 2. It is not in Debian's current archive, so this is
    # a known-awkward step rather than something the script got wrong.
    if ! command -v python2.7 > /dev/null 2>&1; then
        warn "python2.7 is not installed and nexmon's build scripts need it."
        say "    On current Raspberry Pi OS:"
        say "      wget https://www.python.org/ftp/python/2.7.18/Python-2.7.18.tgz"
        say "      # or install it from the Debian archive, then run this again"
        if ! ask "Continue anyway and let the build tell us?" n; then
            exit 1
        fi
    fi
}

# Puts the nexmon_csi checkout in CSI_DIR. A global rather than an echoed path, so that the
# progress output goes to the terminal instead of being captured with it.
CSI_DIR=""

fetch_sources() {
    step "Fetching nexmon and the CSI patch"
    mkdir -p "$PREFIX"

    if [ ! -d "$NEXMON_DIR/.git" ]; then
        say "  cloning nexmon"
        git clone --depth 1 "$NEXMON_REPO" "$NEXMON_DIR" > /dev/null 2>&1 \
            || die "could not clone $NEXMON_REPO"
    fi

    local patch_dir="$NEXMON_DIR/patches/$CHIP/$FW_VERSION"
    mkdir -p "$patch_dir"
    CSI_DIR="$patch_dir/nexmon_csi"

    if [ ! -d "$CSI_DIR/.git" ]; then
        say "  cloning $CSI_REPO ($CSI_BRANCH)"
        git clone --depth 1 --branch "$CSI_BRANCH" "$CSI_REPO" "$CSI_DIR" > /dev/null 2>&1 \
            || die "could not clone $CSI_REPO at branch $CSI_BRANCH"
    else
        say "  updating the CSI patch"
        git -C "$CSI_DIR" fetch --depth 1 origin "$CSI_BRANCH" > /dev/null 2>&1 || true
        git -C "$CSI_DIR" checkout -q FETCH_HEAD > /dev/null 2>&1 || true
    fi

    ok "sources in $NEXMON_DIR"
}

build_firmware() {
    local csi_dir="$CSI_DIR"
    step "Building the patched firmware"
    say "  ${dim}20-40 minutes on a Pi. It compiles a cross-toolchain first.${reset}"

    # setup_env.sh must be sourced from the nexmon root; it exports the paths every
    # downstream Makefile reads.
    ( cd "$NEXMON_DIR" && set +u && . ./setup_env.sh && make ) \
        || die "the nexmon toolchain build failed. This is the fragile step: it is sensitive
       to the kernel and to python2.7 being present. The log above says which.
       Nothing is broken — rerun after fixing and it resumes here."

    # Makefile.rpi is the path that does not need a patched brcmfmac, which is what makes
    # this work on current kernels and on the Pi 5 at all.
    ( cd "$csi_dir" && set +u && . "$NEXMON_DIR/setup_env.sh" \
        && make -f Makefile.rpi && make -f Makefile.rpi install-firmware \
        && make -f Makefile.rpi install-csi-tools ) \
        || die "the CSI firmware build failed; see the log above"

    ok "firmware patched and installed"
}

# ---------------------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------------------

detect_ap() {
    # The BSSID of the current association: the transmitter whose frames we want to measure.
    iw dev "$IFACE" link 2>/dev/null \
        | awk '/Connected to/ {print $3; exit}'
}

install_service() {
    step "Installing the forwarder"

    install -d "$PREFIX/bin"
    install -m 0755 "$SRC_DIR/csi_node.py" "$PREFIX/bin/csi_node.py"

    local csi_dir="$NEXMON_DIR/patches/$CHIP/$FW_VERSION/nexmon_csi"
    local connect="$csi_dir/utils/csi-connected.sh"
    [ -x "$connect" ] || [ -f "$connect" ] || die "missing $connect — was the fork cloned?"
    chmod +x "$connect" 2>/dev/null || true

    if [ -z "$AP_MAC" ]; then
        AP_MAC="$(detect_ap || true)"
    fi
    if [ -z "$AP_MAC" ]; then
        warn "$IFACE is not associated, so there is no access point to measure against.
    Connect it to Wi-Fi and run this again, or pass --ap <bssid>."
    else
        ok "measuring frames from $AP_MAC"
    fi

    if [ -z "$SERVER" ]; then
        SERVER="127.0.0.1"
    fi

    cat > "$ENV_FILE" <<EOF
# Written by pi/install-node.sh. Edit and 'systemctl restart csi-node' to change.
CSI_SERVER_HOST=$SERVER
CSI_UDP_PORT=$PORT
CSI_NODE_ID=$NODE_ID
CSI_IFACE=$IFACE
CSI_PROBE_HZ=$PROBE_HZ
CSI_AP_MAC=$AP_MAC
CSI_CONNECT_SH=$connect
EOF
    chmod 0644 "$ENV_FILE"

    # The extractor configuration does not survive a reboot or a reassociation, so it is set
    # up as part of starting the service rather than once at install time.
    #
    # Through wrapper scripts rather than inline in the unit, because systemd does its own
    # `$VAR` expansion in Exec lines and does not implement the shell's `${VAR:+...}` — an
    # optional argument written inline would silently come out wrong.
    cat > "$PREFIX/bin/capture-up.sh" <<'EOF'
#!/bin/sh
# Configure the nexmon extractor for the current association. Re-run on every service start:
# neither the chanspec nor the collection flag survives a reboot or a reassociation.
set -eu
. /etc/default/csi-node

if [ -z "${CSI_AP_MAC:-}" ]; then
    # Not pinned at install time, so take whatever this interface is associated with now.
    CSI_AP_MAC="$(iw dev "$CSI_IFACE" link 2>/dev/null | awk '/Connected to/ {print $3; exit}')"
fi

if [ -n "${CSI_AP_MAC:-}" ]; then
    exec "$CSI_CONNECT_SH" -i "$CSI_IFACE" -C 0x1 -N 0x1 -m "$CSI_AP_MAC"
fi

echo "csi-node: $CSI_IFACE is not associated; collecting without a transmitter filter" >&2
exec "$CSI_CONNECT_SH" -i "$CSI_IFACE" -C 0x1 -N 0x1
EOF

    cat > "$PREFIX/bin/capture-down.sh" <<'EOF'
#!/bin/sh
# Stop collecting and re-enable scanning, so the Pi can roam normally again.
set -eu
. /etc/default/csi-node
exec "$CSI_CONNECT_SH" -i "$CSI_IFACE" --stop
EOF

    chmod 0755 "$PREFIX/bin/capture-up.sh" "$PREFIX/bin/capture-down.sh"

    cat > "$SERVICE" <<EOF
[Unit]
Description=WiFi CSI node (nexmon_csi capture and forward)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStartPre=$PREFIX/bin/capture-up.sh
ExecStart=/usr/bin/python3 $PREFIX/bin/csi_node.py
ExecStopPost=-$PREFIX/bin/capture-down.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    # A standalone undo, so that removing the node does not depend on still having a checkout
    # of this repo lying around. The top-level install.sh --uninstall calls it if it is there.
    cat > "$PREFIX/uninstall.sh" <<EOF
#!/bin/sh
# Remove the CSI node and put the stock Wi-Fi firmware back. Written by pi/install-node.sh.
set -eu
systemctl disable --now csi-node >/dev/null 2>&1 || true
[ -x "$PREFIX/bin/capture-down.sh" ] && "$PREFIX/bin/capture-down.sh" >/dev/null 2>&1 || true
rm -f "$SERVICE" "$ENV_FILE"
systemctl daemon-reload
update-alternatives --auto brcmfmac43455-sdio.bin >/dev/null 2>&1 || true
echo "csi-node removed. Reboot to load the stock Wi-Fi firmware."
echo "The nexmon build is still in $PREFIX; remove it with: rm -rf $PREFIX $STATE_DIR"
EOF
    chmod 0755 "$PREFIX/uninstall.sh"

    systemctl daemon-reload
    systemctl enable csi-node > /dev/null 2>&1
    ok "csi-node.service installed"
}

start_service() {
    step "Starting the node"
    systemctl restart csi-node || true
    sleep 3
    if systemctl is-active --quiet csi-node; then
        ok "running"
    else
        warn "it did not stay up. The log says why:"
        say "    journalctl -u csi-node -n 40 --no-pager"
    fi
}

uninstall() {
    step "Removing the node"
    systemctl disable --now csi-node > /dev/null 2>&1 || true
    if [ -x "$PREFIX/bin/capture-down.sh" ]; then
        "$PREFIX/bin/capture-down.sh" > /dev/null 2>&1 || true
    fi
    rm -f "$SERVICE" "$ENV_FILE"
    systemctl daemon-reload
    ok "service removed"

    # The patched firmware is installed through update-alternatives, so the stock firmware is
    # still on disk and this puts it back rather than needing a reinstall.
    if command -v update-alternatives > /dev/null 2>&1; then
        update-alternatives --auto brcmfmac43455-sdio.bin > /dev/null 2>&1 || true
    fi
    say ""
    say "The nexmon build in $PREFIX was left alone; remove it with:"
    say "  sudo rm -rf $PREFIX $STATE_DIR"
    say "If Wi-Fi misbehaves, reboot to load the stock firmware."
}

summary() {
    say ""
    say "${bold}The node is up.${reset}"
    say "  systemctl status csi-node"
    say "  journalctl -u csi-node -f     ${dim}(rate, subcarriers, RSSI every 10 s)${reset}"
    say ""
    say "It reports as node ${bold}$NODE_ID${reset} to ${SERVER}:${PORT}. Open the app and look at"
    say "Node health: a steady rate and sub-1% loss means the capture chain works."
    say ""
    say "${dim}Config: $ENV_FILE${reset}"
}

main() {
    say "${bold}WiFi CSI${reset} — Raspberry Pi node"
    say ""

    if [ "$UNINSTALL" = 1 ]; then
        uninstall
        exit 0
    fi

    check_hardware

    if [ "$SKIP_BUILD" = 0 ]; then
        check_pagesize
        if stage_reached "built"; then
            ok "firmware already built; skipping (use --uninstall to start over)"
        else
            install_deps
            fetch_sources
            build_firmware
            stage_done "built"
        fi
    fi

    install_service
    start_service
    summary
}

main "$@"
