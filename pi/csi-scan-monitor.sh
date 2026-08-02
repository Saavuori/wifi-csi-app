#!/bin/sh
# Find the radios in range using the capture radio alone, with no Wi-Fi station on this machine.
#
# The picker needs a list of transmitters to choose from, and nmcli can only produce one from a
# managed Wi-Fi interface. A node whose uplink is eth0 has none: the onboard radio is in monitor
# mode and never associates, and there is no dongle. So the list has to come out of the CSI
# stream itself. Every nexmon packet carries the transmitter's MAC, the chanspec it was heard on
# and a per-frame RSSI, which is a scan if you point the receiver at each channel in turn.
#
# What comes back is not what nmcli hands back, in two ways that the output has to keep saying:
#
#   * BSSIDs, never SSIDs. The extractor only produces CSI for HT/VHT frames — it reads the
#     channel-estimate table, and a legacy frame has only an L-LTF — and measured on this
#     hardware with the transmitter filter removed entirely (25 s, nine access points beaconing
#     in earshot at ~10 Hz each) not one beacon or management frame produced a packet. A
#     network's name only ever travels in a beacon or a probe response. The names are not
#     missing here; they are unobtainable this way.
#   * transmitters, not access points. A block ack from a laptop is an HT frame like any other,
#     so clients appear beside the radios and nothing in the packet separates them: the header
#     carries the first byte of the frame control field, not the ToDS/FromDS bits that would say
#     which end sent it. Measuring a client's uplink is a legitimate measurement, so they are
#     reported rather than guessed away.
#
# A sweep costs measurement. csi-node is stopped for the duration — an unfiltered extractor on a
# hopping radio would feed the node frames from the wrong transmitters on the wrong channel, and
# a link_epoch bump for each — and started again afterwards, which re-arms the capture properly
# through the service's ExecStartPre. Thirteen channels at two seconds is about half a minute of
# silence, which is why csi-ap-agent.sh sweeps on a long interval and on demand, not on a timer
# anything like the nmcli one.
#
# Why the dwell is short: with no -m filter the ucode falls through to DUMP_CSI for every frame
# of 30 bytes or more that it hears, and the receiver is deaf for the duration of each dump. So
# listening longer does not scale the yield the way it would on an ordinary receiver — whoever is
# busy is heard in the first few hundred milliseconds, and the extra seconds mostly buy more deaf
# windows. Two seconds is enough to rank what is in earshot.
#
# Nothing here touches any interface but the capture radio. The only route into this machine is a
# dongle or eth0, and both guards below exist to make sure this is neither: a capture radio is in
# monitor mode and carries no address.
set -eu

CONF=/etc/default/csi-node
if [ -r "$CONF" ]; then . "$CONF"; fi

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

SELF_DIR=$(dirname "$0")
DWELL_PY="${CSI_SCAN_DWELL_PY:-$SELF_DIR/csi_scan_dwell.py}"
DISARM_SH="${CSI_CAPTURE_DOWN_SH:-$SELF_DIR/capture-down.sh}"

IFACE="${CSI_IFACE:-wlan0}"
# The 2.4 GHz plan. 12 and 13 are silent in regulatory domains that forbid them, which costs two
# dwells and reports nothing; dropping them would quietly hide two channels that are legal here.
# 5 GHz channels are accepted too — 20 MHz gives the same 64 subcarriers — but double the sweep.
# 2.4 GHz non-overlapping, then the common 5 GHz allocations. Sweeping all thirteen 2.4 GHz
# channels and no 5 GHz was the obvious plan and the wrong one: measured in this house, every
# 2.4 GHz channel together carried under 7 Hz while 5 GHz channel 40 alone carried 138 Hz, so a
# 2.4-only sweep returns a list with nothing on it worth measuring and hides the one link that
# was. 1/6/11 cover 2.4 GHz between them; anything else there is an overlap of those.
CHANNELS="${CSI_SCAN_CHANNELS:-1 6 11 36 40 44 48 100 149 153 157 161}"
DWELL="${CSI_SCAN_DWELL:-2}"
FORMAT=json

NODE_WAS_ACTIVE=0
ARMED=0
RESTORED=0
TMP=

die() { echo "error: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }

usage() {
    cat <<EOF
Usage: $0 [--iface IF] [--channels LIST] [--dwell SECONDS] [--format FMT]

Build the list of radios in range from the capture radio's own CSI stream, for a node that has
no Wi-Fi station to scan with. Sweeps each channel with the transmitter filter off and counts
who is heard.

  --iface IF       the capture radio, monitor mode (default $IFACE)
  --channels LIST  channels to sweep, comma or space separated (default: $CHANNELS)
  --dwell SECONDS  how long to listen on each channel (default $DWELL)
  --format FMT     json   [{"bssid","channel","signal","frames"}], signal a median RSSI in dBm
                   aps    the same list in the shape csi-ap-agent.sh publishes inside aps.json,
                          where "signal" has to be the 0-100 percentage the web UI prints; the
                          dBm figure is kept alongside it as "rssi_dbm"
                   table  columns, for reading
  -h, --help       this

There are no SSIDs in the output and there cannot be. CSI only exists for HT/VHT frames, a name
only travels in a beacon or a probe response, and beacons produce no CSI — measured here, with
no filter at all, nine access points in earshot and not one beacon captured. Every entry is a
BSSID, and the list is of transmitters heard: client stations appear in it as well as access
points, because nothing in the packet says which end sent the frame.

csi-node is stopped for the duration of the sweep and started again afterwards.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --iface) [ $# -ge 2 ] || die "--iface needs an interface"; IFACE=$2; shift 2 ;;
        --channels) [ $# -ge 2 ] || die "--channels needs a list"; CHANNELS=$2; shift 2 ;;
        --dwell) [ $# -ge 2 ] || die "--dwell needs a number of seconds"; DWELL=$2; shift 2 ;;
        --format) [ $# -ge 2 ] || die "--format needs json, aps or table"; FORMAT=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument '$1'" ;;
    esac
done

case "$FORMAT" in
    json|aps|table) ;;
    *) die "unknown format '$FORMAT' (json, aps or table)" ;;
esac
case "$DWELL" in
    ''|*[!0-9.]*) die "not a number of seconds: '$DWELL'" ;;
esac

CHANNELS=$(printf '%s' "$CHANNELS" | tr ',' ' ')
for ch in $CHANNELS; do
    case "$ch" in
        ''|*[!0-9]*) die "not a channel: '$ch'" ;;
    esac
done
[ -n "$CHANNELS" ] || die "no channels to sweep"

[ "$(id -u)" = 0 ] || die "must run as root"
for tool in nexutil makecsiparams python3; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' is not in PATH"
done
[ -f "$DWELL_PY" ] || die "missing $DWELL_PY, which is what listens on each channel"

# -- refuse to sweep anything that could be the way in --------------------------------------
#
# The capture radio is in monitor mode and has no address; a dongle or eth0 has an address and
# is not in monitor mode. Retuning the wrong interface thirteen times would end the session that
# started this, so this is a hard stop rather than a warning.
ip link show "$IFACE" >/dev/null 2>&1 || die "interface '$IFACE' does not exist"
if ip -4 -o addr show dev "$IFACE" 2>/dev/null | grep -q ' inet '; then
    die "$IFACE has an IPv4 address, so it is carrying the network rather than capturing.
  This sweep retunes the interface it is given; it will not do that to your route in.
  Set CSI_IFACE in $CONF, or pass --iface, to name the monitor-mode capture radio."
fi
if [ "$IFACE" = "${CSI_PROBE_IFACE:-}" ]; then
    die "$IFACE is CSI_PROBE_IFACE, the interface that holds the association"
fi

# Put the radio into monitor mode if it is not already, rather than refusing.
#
# Refusing deadlocks a fresh wired node, and the deadlock is complete: capture-up.sh will not
# start without a chosen BSSID, it is the only thing that puts the radio into monitor mode, and
# the only way to learn a BSSID without an association is this sweep. So the node cannot start
# until it has swept and cannot sweep until it has started, and the operator sees an empty list
# with no way out.
#
# Doing it here is safe precisely because of the checks above: this interface exists, carries no
# address and is not the one holding the association, so it is the capture radio and nothing
# else. Monitor mode needs no chosen transmitter, which is what makes it a bootstrap.
# nexmon's monitor mode is a firmware mode, not an nl80211 interface type: 'nexutil -m1' sets it
# and 'iw dev ... info' still reports "type managed" throughout. Asking iw therefore never
# succeeds on this hardware, so ask the firmware what it thinks it is doing instead.
monitoring() { [ "$(nexutil -I"$IFACE" -m 2>/dev/null | sed -n 's/^monitor: *//p')" != "0" ]; }

if ! monitoring; then
    echo "$IFACE is not in monitor mode; putting it there for the sweep" >&2
    nmcli dev set "$IFACE" managed no 2>/dev/null || true
    ip link set "$IFACE" up 2>/dev/null || true
    nexutil -I"$IFACE" -m1 >/dev/null 2>&1 || true
    sleep 1
    if ! monitoring; then
        die "could not put $IFACE into monitor mode.
  Check that it runs the nexmon CSI firmware and that nexutil is the USE_VENDOR_CMD build:
  'nexutil -I$IFACE -k' must answer with a chanspec."
    fi
    # Nothing was running before, so restore() will disarm rather than hand the radio back --
    # which is what should happen, since leaving an unfiltered extractor armed on an idle radio
    # is the one state worth avoiding.
fi

TMP=$(mktemp -d)
ROWS="$TMP/rows"
: > "$ROWS"

# -- put back what the sweep disturbed --------------------------------------------------------

restore() {
    if [ "$RESTORED" = 1 ]; then
        return 0
    fi
    RESTORED=1
    if [ "$NODE_WAS_ACTIVE" = 1 ]; then
        # Not capture-up.sh directly: starting the service runs it as ExecStartPre with the
        # service's own environment, so the chanspec and the transmitter filter come back exactly
        # as they were configured rather than as this script last left them.
        systemctl start csi-node >/dev/null 2>&1 \
            || warn "could not start csi-node again — 'systemctl start csi-node' by hand"
    elif [ "$ARMED" = 1 ]; then
        # Nothing was running before, so there is nothing to hand the radio back to. Leaving an
        # unfiltered extractor armed is the one state worth avoiding: it dumps for every frame on
        # the channel forever, and the radio stays deaf in bursts with nobody reading the result.
        if [ -x "$DISARM_SH" ]; then
            "$DISARM_SH" >/dev/null 2>&1 || warn "could not disarm the extractor on $IFACE"
        elif [ -n "${CSI_CONNECT_SH:-}" ] && [ -x "${CSI_CONNECT_SH:-}" ]; then
            "$CSI_CONNECT_SH" -i "$IFACE" --stop >/dev/null 2>&1 \
                || warn "could not disarm the extractor on $IFACE"
        else
            warn "the extractor is left armed with no filter on $IFACE; run capture-down.sh"
        fi
    fi
    return 0
}

# Separate from restore(), because the radio is handed back before the result is formatted and
# the rows being formatted live in $TMP.
cleanup() {
    if [ -n "$TMP" ]; then
        rm -rf "$TMP"
    fi
}
trap 'restore; cleanup' EXIT
trap 'restore; cleanup; exit 130' INT TERM HUP

if systemctl is-active --quiet csi-node 2>/dev/null; then
    NODE_WAS_ACTIVE=1
    systemctl stop csi-node >/dev/null 2>&1 || warn "could not stop csi-node; sweeping anyway"
fi

# -- sweep ------------------------------------------------------------------------------------

# The same arm capture-up.sh performs, minus the -m transmitter filter: one core, one spatial
# stream, 20 MHz on the control channel. No 'nexutil -m1' first — that resets the extractor
# configuration and the interface is already in monitor mode, so it would only add a second per
# channel and another piece of interface state to hand back. Writing a new parameter block is
# what retunes the chip: the block carries the chanspec, and without -k the firmware follows it.
arm() {
    ch=$1
    params=$(makecsiparams -c "$ch/20" -C 0x1 -N 0x1 2>/dev/null) || {
        warn "channel $ch: makecsiparams rejected it"
        return 1
    }
    len=$(makecsiparams -c "$ch/20" -C 0x1 -N 0x1 -r 2>/dev/null | wc -c | awk '{print $1}')
    nexutil -I"$IFACE" -s500 -b -l"$len" -v"$params" >/dev/null 2>&1 || true

    # nexutil reports transport failures on stderr and still exits 0, so read csi_collect back
    # out of firmware shared memory. Byte 0 of the ioctl 501 hexdump is the flag.
    collect=$(nexutil -I"$IFACE" -g501 2>/dev/null \
        | sed -n 's/^0x000000: *\([0-9a-f][0-9a-f]\).*/\1/p')
    if [ "$collect" != "01" ]; then
        warn "channel $ch: the extractor did not arm (csi_collect=${collect:-unreadable})"
        return 1
    fi
    ARMED=1
    return 0
}

for ch in $CHANNELS; do
    if arm "$ch"; then
        python3 "$DWELL_PY" --iface "$IFACE" --seconds "$DWELL" --channel "$ch" >> "$ROWS" \
            || warn "channel $ch: the listener failed"
    fi
done

# Hand the radio back before formatting: the node has been silent long enough.
restore

# -- merge and report ---------------------------------------------------------------------------

# One entry per transmitter, on the channel it was heard best. A strong radio bleeds into the
# neighbouring channels — tuned to 4 you still hear channel 6, weaker and less often — and the
# chanspec in the packet is the receiver's, not the transmitter's, so every dwell that hears it
# claims it. Taking the dwell with the most frames (then the strongest) is what picks the channel
# it is actually on. Nothing is medianed across channels: each pair appears in exactly one dwell.
merged() {
    sort -k1,1 -k3,3nr -k4,4nr "$ROWS" | awk '!seen[$1]++ {
        # nmcli reports a 0-100 quality and the web UI prints the number with a % after it, so
        # the percentage has to exist for the "aps" format. -100 dBm -> 0, -50 dBm -> 100, which
        # is the linear map NetworkManager itself uses over the range that matters indoors.
        pct = 2 * ($4 + 100)
        if (pct < 0) pct = 0
        if (pct > 100) pct = 100
        printf "%s %s %s %s %d\n", $1, $2, $3, $4, pct
    }' | sort -k4,4nr
}

NOTE="no SSIDs: beacons carry no CSI, so only BSSIDs are knowable this way, and the list is of transmitters heard — client stations as well as access points"

case "$FORMAT" in
json)
    printf '['
    first=1
    merged | while read -r mac ch frames rssi pct; do
        [ "$first" = 1 ] || printf ','
        first=0
        printf '{"bssid":"%s","channel":%s,"signal":%s,"frames":%s}' "$mac" "$ch" "$rssi" "$frames"
    done
    printf ']\n'
    echo "$NOTE" >&2
    ;;
aps)
    printf '['
    first=1
    merged | while read -r mac ch frames rssi pct; do
        [ "$first" = 1 ] || printf ','
        first=0
        printf '{"bssid":"%s","ssid":"","channel":%s,"signal":%s,"in_use":false,"rssi_dbm":%s,"frames":%s}' \
            "$mac" "$ch" "$pct" "$rssi" "$frames"
    done
    printf ']\n'
    echo "$NOTE" >&2
    ;;
table)
    printf '%-19s %-5s %-8s %s\n' 'BSSID' 'CHAN' 'SIGNAL' 'FRAMES'
    merged | while read -r mac ch frames rssi pct; do
        printf '%-19s %-5s %-8s %s\n' "$mac" "$ch" "${rssi} dBm" "$frames"
    done
    echo
    echo "$NOTE"
    ;;
esac
