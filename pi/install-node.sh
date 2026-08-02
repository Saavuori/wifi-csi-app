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
# THE CAPTURE RADIO CANNOT CARRY TRAFFIC. The onboard BCM43455 captures in monitor mode and
# never associates: while the extractor is armed its ucode deafens the receiver for the duration
# of every CSI dump, so it cannot hold a link that carries traffic. Something else has to be the
# network, and there are two shapes of that:
#
#   --uplink dongle  a USB Wi-Fi dongle holds the association, the route into this machine and
#                    the probe traffic. What is measured can then be derived from its live
#                    association, and re-derived whenever it roams. Nothing installed here ever
#                    reconfigures the dongle.
#   --uplink wired   Ethernet is the backbone and there is no second radio. Strictly safer: no
#                    capture change can cost the route in. It costs the two things that came off
#                    the association -- the transmitter must be chosen by hand (csi-select-ap),
#                    and probing has to be unicast at a wireless client of the measured radio,
#                    because an access point sends broadcast traffic at a legacy rate and the
#                    extractor produces CSI only for HT/VHT frames.
#
# Left unset, the mode is detected: a second wireless interface means dongle, and no second
# radio plus a default route over something else means wired.
#
#     --server HOST      where to send frames (default: this host)
#     --port N           server UDP port (default 5566)
#     --node-id N        node id, 1..254 (default 20)
#     --iface NAME       capture interface, put into monitor mode (default wlan0)
#     --uplink MODE      dongle or wired (default: detected, see above)
#     --probe-iface NAME interface holding the network and sending probes (default: wlan1, or
#                        the wired route's interface in wired mode)
#     --ap MAC           pin the measured transmitter (default: follow --probe-iface)
#     --probe HOST       host to probe so the measured radio transmits (default: the gateway)
#     --probe-mode MODE  broadcast, unicast or icmp (default: broadcast, unicast when wired)
#     --probe-hz N       probe rate (default 100)
#     --max-hz N         cap on the emitted rate, 0 for none (default 100)
#     --fctl BYTE        keep one 802.11 frame class, e.g. 0x94 (default auto)
#     --data-dir PATH    directory the server container mounts as /data (default ~/csi-data)
#     --repo URL         nexmon_csi fork to build
#     --branch NAME      branch of that fork
#     --skip-build       assume the firmware is already patched; just install the service
#     --uninstall        remove the service and restore the stock firmware
#     --yes              assume yes for prompts
#
set -euo pipefail

NEXMON_REPO="https://github.com/seemoo-lab/nexmon.git"
CSI_REPO="${CSI_NEXMON_REPO:-https://github.com/Saavuori/nexmon_csi.git}"
# master rather than the original feature branch: the Pi 5 and current-Raspberry-Pi-OS install
# handling this script relies on (Makefile.rpi, the update-alternatives firmware install) landed
# after that branch, and so did the correction that an armed extractor cannot keep a usable link.
CSI_BRANCH="${CSI_NEXMON_BRANCH:-master}"

# nexmon patches one firmware version per chip directory, and 7_45_189 is the only 43455 image
# with reversed CSI addresses — the hooks in src/ioctl.c and src/csi_extractor.c sit at addresses
# found in that one build. This is not a version Raspberry Pi OS has to be shipping: the patched
# image replaces whatever the distribution installed. Building from a newer directory yields a
# firmware that flashes fine and contains no extractor, which surfaces much later as "tcpdump
# sees no CSI"; Makefile.rpi refuses outright instead.
CHIP="bcm43455c0"
FW_VERSION="7_45_189"

PREFIX="/opt/csi-node"
NEXMON_DIR="$PREFIX/nexmon"
STATE_DIR="/var/lib/csi-node"
STATE_FILE="$STATE_DIR/stage"
ENV_FILE="/etc/default/csi-node"
SERVICE="/etc/systemd/system/csi-node.service"
AGENT_SERVICE="/etc/systemd/system/csi-ap-agent.service"
# NetworkManager must be kept off the capture interface: it would take it back out of monitor
# mode, or try to associate it, either of which ends the capture with nothing to point at.
NM_CONF="/etc/NetworkManager/conf.d/99-csi-node.conf"
NM_DISPATCH="/etc/NetworkManager/dispatcher.d/90-csi-node"
SELECT_BIN="/usr/local/sbin/csi-select-ap"

SERVER=""
PORT="${CSI_UDP_PORT:-5566}"
NODE_ID="${CSI_NODE_ID:-20}"
IFACE="${CSI_IFACE:-wlan0}"
# Empty means "work it out from the hardware"; see detect_uplink.
UPLINK="${CSI_UPLINK:-}"
PROBE_IFACE="${CSI_PROBE_IFACE:-}"
AP_MAC=""
PROBE_HOST="${CSI_PROBE_HOST:-}"
# Empty means "whatever suits the uplink": broadcast has the highest measured yield from an
# associated dongle (74% of sent frames come back as CSI at 100 Hz), and is expected to yield
# nothing at all from the wire.
PROBE_MODE="${CSI_PROBE_MODE:-}"
# Empty means "keep what is configured", which on a first install resolves to 100.
PROBE_HZ="${CSI_PROBE_HZ:-}"
# What arrives is the access point's whole traffic load, so the raw rate follows the household
# and wanders between roughly 100 and 300 Hz. Breathing lives below 0.5 Hz and motion well under
# 10, so none of that is needed; capping below the quiet-hours floor spends the surplus on
# near-uniform spacing instead, which is what the server's resampler wants.
MAX_HZ="${CSI_MAX_HZ:-100}"
FCTL="${CSI_FCTL:-auto}"
# The bridge to the web UI is one shared directory: the server runs in a container and nmcli,
# systemctl and the nexmon tools are all out here on the host.
DATA_DIR="${CSI_DATA_DIR:-}"
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
        --uplink)     UPLINK="${2:?--uplink needs dongle or wired}"; shift ;;
        --probe-iface) PROBE_IFACE="${2:?--probe-iface needs a name}"; shift ;;
        --ap)         AP_MAC="${2:?--ap needs a MAC}"; shift ;;
        --probe)      PROBE_HOST="${2:?--probe needs a host}"; shift ;;
        --probe-mode) PROBE_MODE="${2:?--probe-mode needs broadcast, unicast or icmp}"; shift ;;
        --probe-hz)   PROBE_HZ="${2:?--probe-hz needs a number}"; shift ;;
        --max-hz)     MAX_HZ="${2:?--max-hz needs a number}"; shift ;;
        --fctl)       FCTL="${2:?--fctl needs a byte or 'auto'}"; shift ;;
        --data-dir)   DATA_DIR="${2:?--data-dir needs a path}"; shift ;;
        --repo)       CSI_REPO="${2:?--repo needs a URL}"; shift ;;
        --branch)     CSI_BRANCH="${2:?--branch needs a name}"; shift ;;
        --skip-build) SKIP_BUILD=1 ;;
        --uninstall)  UNINSTALL=1 ;;
        --yes|-y)     ASSUME_YES=1 ;;
        # The whole leading comment block is the help text. Printing it by rule rather than by
        # line number means adding an option to the list above cannot silently truncate --help.
        -h|--help)    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
                      exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || die "this needs root: sudo $0 $*"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Under sudo, $HOME is root's, and the data directory belongs to the user who ran install.sh —
# it is the same ~/csi-data that the compose file bind-mounts into the container as /data.
if [ -z "$DATA_DIR" ]; then
    if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != root ]; then
        DATA_DIR="$(getent passwd "$SUDO_USER" | cut -d: -f6)/csi-data"
    else
        DATA_DIR="$HOME/csi-data"
    fi
fi

# ---------------------------------------------------------------------------------------
# Uplink mode. Which interface is the network decides what this installer can derive, what the
# node can do to induce traffic, and whether putting $IFACE into monitor mode is dangerous.
# ---------------------------------------------------------------------------------------

# The interface carrying the default route. On a wired node that is the entire answer to "what
# still works after $IFACE stops carrying traffic", so it is worth asking rather than assuming.
default_route_iface() {
    ip route show default 2>/dev/null \
        | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }'
}

# A wireless interface that is not the capture radio: a dongle, in other words.
second_radio() {
    local path dev
    for path in /sys/class/net/*/wireless; do
        [ -e "$path" ] || continue
        dev="${path%/wireless}"
        dev="${dev##*/}"
        [ "$dev" = "$IFACE" ] && continue
        printf '%s\n' "$dev"
        return 0
    done
    return 1
}

# Detection rather than preference: the mode describes the hardware. A second radio means the
# two-radio node this was written for. Without one, a default route over something else is what
# makes installing this safe at all, and that is the wired node. Anything else stays 'dongle',
# so that check_hardware warns about the missing radio instead of this quietly deciding that a
# machine with no working network is a wired install.
detect_uplink() {
    if second_radio > /dev/null; then
        printf 'dongle\n'
        return
    fi
    local route
    route="$(default_route_iface)"
    if [ -n "$route" ] && [ "$route" != "$IFACE" ]; then
        printf 'wired\n'
        return
    fi
    printf 'dongle\n'
}

# What is already configured. This script is re-run to upgrade, and it rewrites the config file
# whole, so anything csi-select-ap owns has to be read back first or every upgrade would silently
# undo the choice of transmitter — the one setting here that costs a walk around the house to
# find. A flag on this run still wins over what is on disk.
conf_get() {
    [ -r "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}
PREV_UPLINK="$(conf_get CSI_UPLINK)"
PREV_FOLLOW="$(conf_get CSI_AP_FOLLOW)"
PREV_AP_MAC="$(conf_get CSI_AP_MAC)"
PREV_CHANSPEC="$(conf_get CSI_CHANSPEC)"
PREV_PROBE_IFACE="$(conf_get CSI_PROBE_IFACE)"
PREV_PROBE_HOST="$(conf_get CSI_PROBE_HOST)"
PREV_PROBE_MODE="$(conf_get CSI_PROBE_MODE)"
PREV_PROBE_HZ="$(conf_get CSI_PROBE_HZ)"

DETECTED_UPLINK=0
if [ -z "$UPLINK" ]; then
    # An upgrade keeps the mode it was installed with: detection describes the hardware as it is
    # right now, and a dongle that happens to be unplugged this afternoon is not a decision.
    UPLINK="${PREV_UPLINK:-$(detect_uplink)}"
    [ -n "$PREV_UPLINK" ] || DETECTED_UPLINK=1
fi
case "$UPLINK" in
    dongle|wired) ;;
    *) die "--uplink takes 'dongle' or 'wired', not '$UPLINK'" ;;
esac

# The probe interface is where induced traffic leaves from, and in wired mode also the only route
# into this machine. Follow the uplink unless it was named outright.
#
# A configured one is kept across an upgrade, but only while the uplink stays the same: a wlan1
# carried into wired mode would be an interface that is not the network any more.
if [ -z "$PROBE_IFACE" ] && [ -n "$PREV_PROBE_IFACE" ] && [ "${PREV_UPLINK:-dongle}" = "$UPLINK" ]; then
    PROBE_IFACE="$PREV_PROBE_IFACE"
fi
if [ -z "$PROBE_IFACE" ]; then
    ROUTE_IFACE="$(default_route_iface)"
    if [ "$UPLINK" = wired ]; then
        PROBE_IFACE="${ROUTE_IFACE:-eth0}"
    elif [ -n "$ROUTE_IFACE" ] && [ "$ROUTE_IFACE" != "$IFACE" ] \
         && [ -e "/sys/class/net/$ROUTE_IFACE/wireless" ]; then
        # On a machine with more than one dongle, the radio carrying the route is the one whose
        # association is worth following: it is the one that will still be there tomorrow.
        PROBE_IFACE="$ROUTE_IFACE"
    else
        PROBE_IFACE="$(second_radio || true)"
        PROBE_IFACE="${PROBE_IFACE:-wlan1}"
    fi
fi

if [ -z "$PROBE_MODE" ]; then
    # Keep a configured mode, unless the uplink itself is changing: a probe mode chosen for a
    # dongle is not a decision anybody made about the wire.
    if [ -n "$PREV_PROBE_MODE" ] && [ "${PREV_UPLINK:-dongle}" = "$UPLINK" ]; then
        PROBE_MODE="$PREV_PROBE_MODE"
    elif [ "$UPLINK" = wired ]; then
        PROBE_MODE="unicast"
    else
        PROBE_MODE="broadcast"
    fi
fi
case "$PROBE_MODE" in
    broadcast|unicast|icmp) ;;
    *) die "--probe-mode takes broadcast, unicast or icmp, not '$PROBE_MODE'" ;;
esac

PROBE_HOST="${PROBE_HOST:-$PREV_PROBE_HOST}"
PROBE_HZ="${PROBE_HZ:-${PREV_PROBE_HZ:-100}}"

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

    if [ "$DETECTED_UPLINK" = 1 ]; then
        say "  uplink: $UPLINK (detected; pass --uplink to override)"
    else
        say "  uplink: $UPLINK"
    fi

    # Whatever the mode, the same question has to be answered: after $IFACE goes into monitor
    # mode it stops carrying traffic entirely, so something else must already be the network.
    # Once the extractor is armed the onboard chip goes deaf for the duration of every CSI dump
    # — measured in associated mode, DHCP never completes and pings never arrive.
    if [ "$UPLINK" = wired ]; then
        local route
        route="$(default_route_iface)"
        if [ -z "$route" ]; then
            warn "wired uplink was asked for, but this machine has no default route at all, so
    there is nothing to be the network once $IFACE stops carrying traffic. Plug in
    Ethernet first, or use --uplink dongle with a USB Wi-Fi dongle."
            ask "Continue anyway?" n || die "stopping before the network goes away"
        elif [ "$route" = "$IFACE" ]; then
            warn "wired uplink was asked for, but the default route runs over $IFACE — the
    interface about to go into monitor mode. That is the dangerous case wearing the
    safe one's clothes: install this and the machine goes off the network, taking
    your way back in with it. Plug in Ethernet first."
            ask "Continue anyway?" n || die "stopping before the network goes away"
        else
            ok "$route carries the default route; $IFACE is free to capture"
        fi
        if [ -n "$route" ] && [ "$route" != "$PROBE_IFACE" ]; then
            warn "probes will be sent from $PROBE_IFACE, which is not the interface carrying
    the default route ($route). Pass --probe-iface if that is not deliberate."
        fi
    elif [ ! -d "/sys/class/net/$PROBE_IFACE" ]; then
        warn "no interface $PROBE_IFACE, which is where the association and the route into
    this machine are supposed to live. $IFACE goes into monitor mode and stops
    carrying traffic entirely, so if $IFACE is how you are connected right now, you
    will lose this session. Plug in a USB Wi-Fi dongle, or run this with
    --uplink wired once Ethernet is carrying the network."
        ask "Continue anyway?" n || die "stopping before the network goes away"
    else
        ok "$PROBE_IFACE holds the association; $IFACE is free to capture"
    fi

    # nmcli is how the picker finds radios — but only where there is a managed Wi-Fi interface
    # to scan with. A wired node has none, so its list comes from the monitor radio's own sweep
    # and nmcli is simply not part of the picture.
    if [ "$UPLINK" = wired ]; then
        say "  the radios in range come from ${IFACE}'s own sweep, published as aps.json;
    beacons yield no CSI, so that list has BSSIDs and no names."
    else
        command -v nmcli > /dev/null 2>&1 \
            || warn "nmcli is missing, so the access point picker will not work. The node itself
    is unaffected: it will follow $PROBE_IFACE."
    fi
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
    # The BSSID the dongle is associated to. Only a starting point: capture-up.sh re-reads it
    # on every service start, because a value pinned at install time is wrong the moment the
    # dongle roams, and a stale transmitter filter reads as a perfectly quiet room.
    iw dev "$PROBE_IFACE" link 2>/dev/null \
        | awk '/Connected to/ {print $3; exit}'
}

install_service() {
    step "Installing the forwarder"

    install -d "$PREFIX/bin"
    # The helper scripts are files in this repo rather than heredocs in this installer: they are
    # what runs on the Pi, they carry the reasoning for what they do, and reviewing a shell
    # script quoted inside another shell script is how the wrong thing gets shipped.
    install -m 0755 "$SRC_DIR/csi_node.py"      "$PREFIX/bin/csi_node.py"
    install -m 0755 "$SRC_DIR/capture-up.sh"    "$PREFIX/bin/capture-up.sh"
    install -m 0755 "$SRC_DIR/run-node.sh"      "$PREFIX/bin/run-node.sh"
    install -m 0755 "$SRC_DIR/csi-ap-agent.sh"  "$PREFIX/bin/csi-ap-agent.sh"
    # How a node with no Wi-Fi station finds the radios in range. Installed either way: the
    # agent only reaches for it when CSI_UPLINK=wired, and it is worth having at a prompt on a
    # node with a dongle too. csi_scan_dwell.py has to land beside csi_node.py, whose packet
    # decoder it imports rather than repeating.
    install -m 0755 "$SRC_DIR/csi-scan-monitor.sh" "$PREFIX/bin/csi-scan-monitor.sh"
    install -m 0755 "$SRC_DIR/csi_scan_dwell.py"   "$PREFIX/bin/csi_scan_dwell.py"
    # On PATH and without the extension: csi-ap-agent.sh invokes it by bare name, and so does
    # anyone at a prompt.
    install -m 0755 "$SRC_DIR/csi-select-ap.sh" "$SELECT_BIN"

    local csi_dir="$NEXMON_DIR/patches/$CHIP/$FW_VERSION/nexmon_csi"
    local connect="$csi_dir/utils/csi-connected.sh"
    [ -x "$connect" ] || [ -f "$connect" ] || die "missing $connect — was the fork cloned?"
    chmod +x "$connect" 2>/dev/null || true

    # Which transmitter, and how the answer was arrived at. The channel travels with it: a BSSID
    # without one is not a usable selection, and the only place a channel is known here is a
    # previous run's config — csi-select-ap is what discovers new ones.
    local ap_follow="manual"
    local chanspec=""
    if [ -n "$AP_MAC" ]; then
        # An explicit --ap keeps the channel only if it is the same radio the config already
        # named; for any other one there is nothing to go on and csi-select-ap has to be run.
        [ "$AP_MAC" = "$PREV_AP_MAC" ] && chanspec="$PREV_CHANSPEC"
    elif [ "$UPLINK" = wired ]; then
        # Nothing to follow and nothing to detect: without an association there is no BSSID to
        # read and no channel to copy, so a wired node can only keep what was chosen before.
        AP_MAC="$PREV_AP_MAC"
        chanspec="$PREV_CHANSPEC"
    elif [ "$PREV_FOLLOW" = manual ] && [ -n "$PREV_AP_MAC" ]; then
        # An upgrade must not undo a selection. Re-running this script is the documented way to
        # pick up a new build, and it would be a poor trade for the node to come back measuring
        # a different room than it did five minutes ago.
        AP_MAC="$PREV_AP_MAC"
        chanspec="$PREV_CHANSPEC"
    else
        ap_follow="auto"
        AP_MAC="$(detect_ap || true)"
    fi

    if [ "$UPLINK" = wired ] && { [ -z "$AP_MAC" ] || [ -z "$chanspec" ]; }; then
        warn "no transmitter is chosen, and a wired node cannot derive one — there is no
    association to read a BSSID and a channel off. The node will refuse to start
    rather than capture a channel nobody asked for. Choose one once this finishes:
        csi-select-ap --list
        csi-select-ap <bssid> --probe-mode unicast --probe <a client of that radio>"
    elif [ -z "$AP_MAC" ]; then
        warn "$PROBE_IFACE is not associated, so there is nothing to derive a transmitter and
    a channel from yet. capture-up.sh will try again at every start; connect the
    dongle, or pass --ap <bssid>."
    elif [ "$ap_follow" = auto ]; then
        ok "following $PROBE_IFACE, currently associated to $AP_MAC"
    elif [ -n "$chanspec" ]; then
        ok "pinned to $AP_MAC on $chanspec"
    else
        ok "pinned to $AP_MAC; its channel is unknown here, so run csi-select-ap $AP_MAC"
    fi

    if [ -z "$SERVER" ]; then
        SERVER="127.0.0.1"
    fi

    install -d -m 0755 "$DATA_DIR"

    cat > "$ENV_FILE" <<EOF
# Written by pi/install-node.sh. Edit and 'systemctl restart csi-node' to change.
#
# csi-select-ap rewrites CSI_AP_FOLLOW, CSI_AP_MAC, CSI_CHANSPEC, CSI_PROBE_HOST, CSI_PROBE_MODE
# and CSI_PROBE_HZ in place when a radio is chosen, so hand edits to those can be overwritten.
# Re-running the installer keeps whatever those hold rather than resetting them.

# Where frames go.
CSI_SERVER_HOST=$SERVER
CSI_UDP_PORT=$PORT
CSI_NODE_ID=$NODE_ID

# Which interface is the network, and so what this node can work out for itself.
#
# 'dongle': a USB radio holds the association, the route in and the probes. What to measure is
# derived from its live association and re-derived whenever it roams.
# 'wired':  Ethernet is the backbone and no interface on this machine is associated to anything.
# Safer, because no capture change can cost the route in — and two things stop being automatic.
# There is no association to derive a transmitter from, so CSI_AP_MAC and CSI_CHANSPEC must be
# set (capture-up.sh refuses to start otherwise, rather than capturing a channel nobody asked
# for); and probing has to be unicast, because an access point sends group-addressed frames at a
# legacy rate and the extractor only produces CSI for HT/VHT.
CSI_UPLINK=$UPLINK

# The capture radio. Monitor mode, never associated: while the extractor is armed the ucode
# deafens the receiver for the duration of every CSI dump, so this interface cannot carry
# traffic. Do not put your network on it.
CSI_IFACE=$IFACE
# Filled in by capture-up.sh in auto mode; set by csi-select-ap otherwise. Always the control
# channel at 20 MHz, which is 64 subcarriers — the layout the DSP was tuned on.
CSI_CHANSPEC=$chanspec

# Which transmitter to measure. 'auto' re-derives it from $PROBE_IFACE at every start and on
# every roam; 'manual' means it was chosen by hand and may well be a radio this machine is not
# associated to at all — which is usually the point. In a mesh the nearest access point is the
# worst sensor: what is measured is the path between a transmitter and whatever it transmits
# to, so a unit in the same room with nothing moving between gives a perfectly static channel.
# On a wired node only 'manual' means anything: there is no association to follow.
CSI_AP_FOLLOW=$ap_follow
CSI_AP_MAC=$AP_MAC

# The interface that holds the network, the route into this machine, and the probes. Never the
# capture radio — that one cannot transmit. Choosing what to measure never touches this, which
# is what makes the picker safe on a machine reachable only over it.
CSI_PROBE_IFACE=$PROBE_IFACE
# The host to probe so the measured radio transmits. It has to be a host associated to *that*
# radio: replies come from whichever radio the host is on, and replies from any other one are
# dropped by the transmitter filter, leaving only whatever traffic the household already sends
# through it. Empty means the default gateway, which is only right when measuring the radio
# $PROBE_IFACE itself uses.
CSI_PROBE_HOST=$PROBE_HOST
# How to induce that traffic. Measured from an associated dongle: broadcast at 100 Hz sent gave
# +78 Hz captured (74%), while ICMP echo at 200 Hz gave about +14 Hz, because replies aggregate
# and acknowledgements do not. From the wire broadcast is expected to give nothing at all — the
# access point puts group-addressed frames on the air at a legacy rate, which carries no CSI —
# so a wired node uses unicast at one of that radio's own clients. That path is reasoned, not
# yet measured: watch the rate after changing it rather than assuming it works.
CSI_PROBE_MODE=$PROBE_MODE
CSI_PROBE_HZ=$PROBE_HZ

# A cap on the emitted rate, not a target. What arrives is the access point's whole traffic
# load — measured 201 Hz with no probing at all against 215 Hz with 200 Hz of probes, because
# one block ack covers a whole aggregated burst — so the raw rate follows the household and
# wanders. Capping below its quiet-hours floor trades a surplus nothing needs for near-uniform
# spacing, which is what the server's resampler wants.
CSI_MAX_HZ=$MAX_HZ

# Keep one 802.11 frame class. Measured on this link: 98% block acks (0x94), 1.8% QoS data
# (0x88), and the two correlate only 0.918 with each other while each self-correlates above
# 0.99 — so mixing them puts a stray row into the waterfall every time the rare kind appears.
# Locking onto one class cut waterfall spikes from 5.19% of frames to 1.68%. 'auto' spends the
# first couple of seconds counting and keeps whichever dominates.
CSI_FCTL=$FCTL

# The host directory the server container has bind-mounted as /data. csi-ap-agent.sh publishes
# aps.json there and picks up selection requests from it; that shared directory is the whole
# bridge between a container and the host tools a selection needs.
CSI_DATA_DIR=$DATA_DIR

# nexmon_csi's utils/csi-connected.sh. Only used to disarm the extractor on stop; arming is
# done directly by capture-up.sh, which needs monitor mode rather than an association.
CSI_CONNECT_SH=$connect
EOF
    chmod 0644 "$ENV_FILE"

    # Stopping is still the fork's job: --stop writes csi_collect=0, which is also what makes
    # the firmware re-enable scanning. Left armed, the radio stays deaf in bursts forever.
    cat > "$PREFIX/bin/capture-down.sh" <<'EOF'
#!/bin/sh
# Disarm the extractor, so the capture radio stops going deaf and scanning is re-enabled.
set -eu
. /etc/default/csi-node
exec "$CSI_CONNECT_SH" -i "$CSI_IFACE" --stop
EOF
    chmod 0755 "$PREFIX/bin/capture-down.sh"

    # NetworkManager has to be kept off the capture interface permanently. Left managed it will
    # take the interface back out of monitor mode or try to associate it, and either one ends
    # the capture with no error anywhere — the node simply reports zero frames.
    install -d "$(dirname "$NM_CONF")"
    cat > "$NM_CONF" <<EOF
# Written by pi/install-node.sh.
# $IFACE is the CSI capture radio: monitor mode, retuned by capture-up.sh, no association ever.
[keyfile]
unmanaged-devices=interface-name:$IFACE
EOF
    chmod 0644 "$NM_CONF"

    # Re-derive the capture when the dongle moves. The capture filters one transmitter on one
    # channel, both read from the dongle's association at service start; if the dongle later
    # reconnects somewhere else the filter keeps matching a radio that is no longer talking to
    # us and the node goes silent with nothing obviously wrong.
    #
    # Installed in wired mode too, where it reads CSI_UPLINK and exits immediately: switching a
    # node back to a dongle is then an edit and a restart rather than a reinstall.
    install -d "$(dirname "$NM_DISPATCH")"
    install -m 0755 -o root -g root "$SRC_DIR/90-csi-node" "$NM_DISPATCH"

    cat > "$SERVICE" <<EOF
[Unit]
Description=WiFi CSI node (nexmon_csi monitor capture and forward)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
# Neither the chanspec nor the collection flag survives a reboot or a reassociation, so the
# extractor is armed as part of starting rather than once at install time. run-node.sh then
# layers /run/csi-node.env over the static config, so the node's software transmitter filter
# always matches the firmware's — if those two disagree, every frame is discarded.
ExecStartPre=$PREFIX/bin/capture-up.sh
ExecStart=$PREFIX/bin/run-node.sh
ExecStopPost=-$PREFIX/bin/capture-down.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    # The picker's host half. It publishes what is in range into the shared data directory and
    # carries out the selections the web UI asks for; the container has no way to run nmcli or
    # systemctl itself, and is given none. A request can move what is measured and can never
    # touch the dongle's association, so it cannot cost us the machine.
    cat > "$AGENT_SERVICE" <<EOF
[Unit]
Description=WiFi CSI access point picker (host side of the web UI bridge)
After=csi-node.service NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$PREFIX/bin/csi-ap-agent.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    # A standalone undo, so that removing the node does not depend on still having a checkout
    # of this repo lying around. The top-level install.sh --uninstall calls it if it is there.
    cat > "$PREFIX/uninstall.sh" <<EOF
#!/bin/sh
# Remove the CSI node and put the stock Wi-Fi firmware back. Written by pi/install-node.sh.
set -eu
systemctl disable --now csi-node csi-ap-agent >/dev/null 2>&1 || true
[ -x "$PREFIX/bin/capture-down.sh" ] && "$PREFIX/bin/capture-down.sh" >/dev/null 2>&1 || true
rm -f "$SERVICE" "$AGENT_SERVICE" "$ENV_FILE" "$NM_CONF" "$NM_DISPATCH" "$SELECT_BIN"
systemctl daemon-reload
systemctl reload NetworkManager >/dev/null 2>&1 || true
update-alternatives --auto brcmfmac43455-sdio.bin >/dev/null 2>&1 || true
echo "csi-node removed. Reboot to load the stock Wi-Fi firmware."
echo "The nexmon build is still in $PREFIX; remove it with: rm -rf $PREFIX $STATE_DIR"
EOF
    chmod 0755 "$PREFIX/uninstall.sh"

    # So that $IFACE is released now rather than at the next boot.
    systemctl reload NetworkManager > /dev/null 2>&1 || true
    systemctl daemon-reload
    systemctl enable csi-node > /dev/null 2>&1
    systemctl enable csi-ap-agent > /dev/null 2>&1
    ok "csi-node.service and csi-ap-agent.service installed"
}

start_service() {
    step "Starting the node"
    systemctl restart csi-node || true
    sleep 3
    if systemctl is-active --quiet csi-node; then
        ok "running"
    elif [ "$UPLINK" = wired ] \
         && { [ -z "$(conf_get CSI_AP_MAC)" ] || [ -z "$(conf_get CSI_CHANSPEC)" ]; }; then
        # Not a broken install, and worth saying so before the reader goes log-diving: a wired
        # node has nothing to derive a transmitter from, so it refuses to start rather than
        # capture a channel nobody chose. It comes up as soon as one is set.
        warn "the node is waiting for a transmitter, which is where a wired node sits until
    csi-select-ap has been run. Pick one and it starts:
        csi-select-ap --list
        csi-select-ap <bssid> --probe-mode unicast --probe <a client of that radio>"
    else
        warn "it did not stay up. The log says why:"
        say "    journalctl -u csi-node -n 40 --no-pager"
    fi

    systemctl restart csi-ap-agent || true
    if systemctl is-active --quiet csi-ap-agent; then
        ok "access point picker publishing to $DATA_DIR/aps.json"
    elif [ "$UPLINK" = wired ]; then
        # On a wired node the agent is not a convenience: it is the only thing that publishes
        # the sweep a transmitter can be chosen from, since there is no interface to scan with.
        warn "the picker did not start, so nothing is publishing the list a wired node picks
    its transmitter from."
        say "    journalctl -u csi-ap-agent -n 20 --no-pager"
    else
        warn "the picker did not start; the node is unaffected and follows $PROBE_IFACE."
        say "    journalctl -u csi-ap-agent -n 20 --no-pager"
    fi
}

uninstall() {
    step "Removing the node"
    systemctl disable --now csi-node csi-ap-agent > /dev/null 2>&1 || true
    if [ -x "$PREFIX/bin/capture-down.sh" ]; then
        "$PREFIX/bin/capture-down.sh" > /dev/null 2>&1 || true
    fi
    rm -f "$SERVICE" "$AGENT_SERVICE" "$ENV_FILE" "$NM_CONF" "$NM_DISPATCH" "$SELECT_BIN"
    systemctl daemon-reload
    # Hand the capture interface back to NetworkManager, or it stays unmanaged and unused.
    systemctl reload NetworkManager > /dev/null 2>&1 || true
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
    # Do not claim it is running when it is not: on a wired node with no transmitter chosen yet,
    # "the node is up" is exactly the sentence that stops someone reading the next paragraph.
    if systemctl is-active --quiet csi-node; then
        say "${bold}The node is up.${reset}"
    else
        say "${bold}The node is installed.${reset}"
    fi
    say "  systemctl status csi-node"
    say "  journalctl -u csi-node -f     ${dim}(rate, subcarriers, RSSI every 10 s)${reset}"
    say ""
    say "It reports as node ${bold}$NODE_ID${reset} to ${SERVER}:${PORT}. Open the app and look at"
    say "Node health: a steady rate and sub-1% loss means the capture chain works."
    say ""
    say "${bold}Which radio it measures matters more than it sounds.${reset} In a mesh the nearest"
    say "access point is usually the worst sensor — what is measured is the path between a"
    say "transmitter and whatever it is transmitting to, so a unit in the same room with"
    say "nothing moving between gives a beautiful, perfectly static channel."
    say ""
    say "  csi-select-ap --list"
    say "  csi-select-ap <bssid> --probe <a host associated to that radio>"
    if [ "$UPLINK" = wired ]; then
        say "  csi-select-ap --probe-mode unicast --probe HOST   ${dim}(change only the probing)${reset}"
        say ""
        say "${bold}This is a wired node${reset}, so that choice is not optional: with no association"
        say "there is nothing to derive a transmitter from, and the node refuses to start"
        say "until one is set. The list comes from ${IFACE}'s own sweep and carries BSSIDs but"
        say "no names — an SSID travels in beacons, and beacons yield no CSI."
        say ""
        say "Probing has to be ${bold}unicast at a wireless client of the measured radio${reset}: broadcast"
        say "leaves an access point at a legacy rate, which carries no CSI at all, so probing"
        say "from the wire in that mode is expected to induce nothing. That reasoning has not"
        say "been measured on this hardware — watch the rate in the log and treat a rate that"
        say "does not move when the probe rate changes as the probing doing nothing."
    else
        say "  csi-select-ap --auto           ${dim}(back to whatever $PROBE_IFACE uses)${reset}"
        say ""
        say "  ${dim}Safe on any machine: picking a transmitter moves $IFACE, never $PROBE_IFACE,${reset}"
        say "  ${dim}so it cannot cost you the route in. The same choice is in the web UI.${reset}"
    fi
    say ""
    say "${dim}Config: $ENV_FILE${reset}  ${dim}(uplink: $UPLINK)${reset}"
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
