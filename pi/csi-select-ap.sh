#!/bin/sh
# Choose which radio the node measures.
#
# In a mesh the nearest access point is usually the worst sensor. What a CSI node measures is
# the path between one transmitter and whatever it is transmitting to, so the useful question is
# not "which radio is strongest" but "which radio's traffic crosses the space I care about". A
# unit in the same room as the Pi, with nothing moving between them, has a beautiful and
# perfectly static channel.
#
# The capture radio is independent of the dongle: wlan0 can monitor any channel while wlan1
# stays associated wherever it likes. So picking a transmitter never touches the association --
# and on a machine whose only route in is that dongle, that is the whole point. Selecting an
# access point cannot cost you the machine.
#
# Two things follow from a choice:
#   * the capture radio moves to that BSSID's channel
#   * something has to make that radio transmit. If it is the one the dongle is associated to,
#     probing the network does it. If it is another unit, pick a host associated to *that* unit
#     and probe that instead: the access point has to put every reply on the air, and the reply
#     is what we measure. Without that you get only whatever the household already sends through
#     it -- and not its beacons, which the extractor never dumps CSI for.
#
# Both halves of that are why --probe-mode lives here too: what is measured and what induces it
# are one decision, and splitting them across two files is how a node ends up pointed at a radio
# it is doing nothing to make talk.
#
# On a wired node (CSI_UPLINK=wired) there is no dongle, so there is no managed Wi-Fi interface
# for nmcli to scan with either. Discovery then comes from the monitor radio itself and reaches
# this script as the aps.json csi-ap-agent publishes; --list reads that file instead of scanning.
set -eu

CONF=/etc/default/csi-node
RUN_ENV=/run/csi-node.env
PROBE_IFACE=$(sed -n 's/^CSI_PROBE_IFACE=//p' "$CONF" 2>/dev/null || true)
PROBE_IFACE="${PROBE_IFACE:-wlan1}"
UPLINK=$(sed -n 's/^CSI_UPLINK=//p' "$CONF" 2>/dev/null || true)
UPLINK="${UPLINK:-dongle}"
# The shared directory the server container mounts as /data, which is also where the published
# sweep lands. There is no default worth guessing: reading a made-up path would present as "no
# radios in range" on a node whose agent is publishing perfectly well somewhere else.
DATA_DIR=$(sed -n 's/^CSI_DATA_DIR=//p' "$CONF" 2>/dev/null || true)
APS_JSON="$DATA_DIR/aps.json"

usage() {
    cat <<EOF
Usage: $0 --list
       $0 <bssid|index> [--probe HOST] [--probe-mode MODE] [--probe-hz N]
       $0 --auto [--probe HOST] [--probe-mode MODE] [--probe-hz N]
       $0 [--probe HOST] [--probe-mode MODE] [--probe-hz N]
       $0 --status

  --list          the radios in range, their channel and signal. With a dongle this is a scan;
                  on a wired node it is the monitor sweep csi-ap-agent publishes, which sees
                  BSSIDs but no names -- beacons carry the SSID and yield no CSI.
  <bssid>         measure this transmitter; also accepts an index from --list
  --probe HOST    host to probe so the chosen radio transmits. Must be associated to *that*
                  radio, or its replies come from a different one and are filtered out.
                  Unset means the default gateway, which is right only when the measured radio
                  is the one this machine's own traffic goes through -- and never on a wired
                  node, where the gateway is reached over the wire and nothing goes on the air.
  --probe-mode M  how to induce that traffic: broadcast (UDP to the subnet broadcast address --
                  measured best from an associated dongle, 74% of sent frames come back as
                  captured CSI), unicast (UDP at one host, which the access point must deliver
                  over the air; the only mode that can work from a wired node) or icmp.
  --probe-hz N    probe rate, 0 to measure only what the network already carries
  --auto          go back to measuring whatever $PROBE_IFACE is associated to (needs a dongle)
  --status        what is being measured now, and whether frames are arriving

Given no transmitter, only the probing is changed and the chosen radio is left alone.

Examples:
  $0 --list
  $0 7A:DA:88:A3:03:4B --probe 192.168.0.121   # far unit, driven via one of its clients
  $0 --probe-mode unicast --probe 192.168.0.121 # same radio, change only how it is induced
  $0 --auto                                     # back to the dongle's own access point
EOF
}

lower() { printf '%s' "$1" | tr 'A-F' 'a-f'; }

# One record format for both discovery paths -- IN-USE:BSSID:SSID:CHAN:SIGNAL, with the colons
# inside a BSSID written as '-' -- so that listing and resolving are indifferent to where the
# radios were found. That is nmcli -t's own shape; the wired path is made to match it rather
# than the other way round, because nmcli is the one we do not get to choose.
scan() {
    if [ "$UPLINK" = wired ]; then
        scan_published
    else
        scan_nmcli
    fi
}

# nmcli -t escapes the colons inside a BSSID as '\:'. Turn those into '-' so the remaining
# colons are unambiguous field separators, and turn them back when a real MAC is needed.
scan_nmcli() {
    nmcli -t -f IN-USE,BSSID,SSID,CHAN,SIGNAL dev wifi list ifname "$PROBE_IFACE" --rescan auto \
        2>/dev/null | sed 's/\\:/-/g'
}

# A wired node has no managed Wi-Fi interface, so nmcli has nothing to scan with: discovery has
# to come from the monitor radio, and it arrives here as the file csi-ap-agent publishes. Read
# it rather than reproducing the sweep -- the sweep costs the capture its channel for as long as
# it runs, and doing that twice for one --list is a gap in the data for nothing.
#
# Names are normally empty in this path and that is not a fault: the extractor only dumps CSI for
# HT/VHT frames, and an SSID travels in beacons, which are neither.
scan_published() {
    if [ -z "$DATA_DIR" ]; then
        echo "error: CSI_DATA_DIR is unset in $CONF, so there is nowhere to read the published" \
             "sweep from. Set it to the directory the server container mounts as /data." >&2
        return 1
    fi
    if [ ! -r "$APS_JSON" ]; then
        echo "error: no sweep at $APS_JSON yet. csi-ap-agent writes it; check" \
             "'systemctl status csi-ap-agent'." >&2
        return 1
    fi

    # Flatten first, then let braces become newlines: that puts one JSON object on each line
    # without needing a parser, and it holds whether the file is the agent's compact output or
    # something that pretty-printed it. Splitting the raw file on braces alone would work on the
    # first and quietly find nothing in the second, which is the kind of failure that reads as
    # "no radios in range".
    #
    # The document also carries a "measured" object with a bssid in it, so keep only the records
    # that have a channel as well -- "measured" has a chanspec, which is not the same field.
    tr -d '\r\n' < "$APS_JSON" | tr '{}' '\n\n' | while read -r obj; do
        case "$obj" in *'"bssid"'*) ;; *) continue ;; esac
        case "$obj" in *'"channel"'*) ;; *) continue ;; esac

        bssid=$(printf '%s' "$obj" | sed -n 's/.*"bssid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
        ssid=$(printf '%s' "$obj" | sed -n 's/.*"ssid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
        chan=$(printf '%s' "$obj" | sed -n 's/.*"channel"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p')
        signal=$(printf '%s' "$obj" | sed -n 's/.*"signal"[[:space:]]*:[[:space:]]*\(-\{0,1\}[0-9]\{1,\}\).*/\1/p')

        # A record missing either of the two fields a selection needs is worse than absent: it
        # would take an index in --list and resolve to a half-written config.
        [ -n "$bssid" ] && [ -n "$chan" ] || continue

        # Nothing is "in use" without an association, so that column stays empty here. Colons in
        # a name are escaped the way nmcli escapes them, for the same reason.
        printf ':%s:%s:%s:%s\n' \
            "$(printf '%s' "$bssid" | tr ':' '-')" \
            "$(printf '%s' "$ssid" | tr ':' '-')" \
            "$chan" "${signal:-0}"
    done
}

list() {
    current=$(sed -n 's/^CSI_AP_MAC=//p' "$RUN_ENV" 2>/dev/null || true)
    inuse_mark=''
    # An empty name means two different things. A scan that saw a beacon with no SSID in it found
    # a hidden network; a monitor sweep never sees a beacon at all, so on a wired node the name
    # is not hidden, it is unobtainable, and saying "hidden" would send someone looking for it.
    noname='<hidden>'
    [ "$UPLINK" = wired ] && noname='<no name>'

    # Scan once into a variable rather than piping the function twice: with a dongle each call
    # is a real scan that takes the radio off channel, and an empty result has to be told apart
    # from a failed one before anything is printed.
    out=$(scan) || return 1

    printf '%-3s %-19s %-20s %-5s %-7s %s\n' '#' 'BSSID' 'SSID' 'CHAN' 'SIGNAL' 'NOTE'
    i=0
    printf '%s\n' "$out" | grep -v '^$' | while IFS=: read -r inuse bssid ssid chan signal; do
        i=$((i + 1))
        mac=$(printf '%s' "$bssid" | tr '-' ':')
        note=''
        [ "$inuse" = '*' ] && note='dongle associated here'
        if [ -n "$current" ] && [ "$(lower "$mac")" = "$(lower "$current")" ]; then
            note="${note:+$note, }measured now"
        fi
        printf '%-3s %-19s %-20s %-5s %-7s %s\n' \
            "$i" "$mac" "${ssid:-$noname}" "$chan" "$signal" "$note"
    done

    # An empty list is a state, not an absence of output: on a wired node the first sweep has
    # usually not finished yet, and on either kind a picker showing nothing invites a hunt for a
    # broken pipe that is not there.
    if [ -z "$out" ]; then
        echo
        if [ "$UPLINK" = wired ]; then
            echo "Nothing in the list yet. csi-ap-agent sweeps at startup and on demand rather"
            echo "than on a timer -- a sweep stops the capture for about half a minute -- so the"
            echo "first one can be a minute away: journalctl -u csi-ap-agent -f"
        else
            echo "Nothing in range, which usually means $PROBE_IFACE is not up or not associated."
        fi
        return 0
    fi

    # A wired list can be a quarter of an hour old -- a sweep stops the capture for about half a
    # minute, so the agent does not run one often -- and it lists transmitters rather than access
    # points. Both change what the reader should make of it, so both are said with it.
    if [ "$UPLINK" = wired ] && [ -r "$APS_JSON" ]; then
        scanned=$(sed -n 's/.*"scanned_at"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' \
                  "$APS_JSON" 2>/dev/null | head -1)
        case "${scanned:-0}" in ''|*[!0-9]*) scanned=0 ;; esac
        echo
        if [ "$scanned" -gt 0 ]; then
            echo "from a monitor sweep $(( ($(date +%s) - scanned) / 60 )) minutes ago."
        else
            echo "from a monitor sweep of the capture radio."
        fi
        echo "Names are unobtainable this way: a name travels in a beacon and beacons yield no"
        echo "CSI. These are transmitters heard, so client stations appear beside access points."
    fi
    unset inuse_mark
}

# Echoes "<bssid> <channel>". Every field is validated, because a half-parsed scan line written
# into the config is invisible: capture-up.sh falls back to following the dongle and the node
# keeps producing frames from the wrong radio.
resolve() {
    want=$1
    case "$want" in
        [0-9]|[0-9][0-9]) line=$(scan | sed -n "${want}p") ;;
        *) line=$(scan | grep -i -- "$(printf '%s' "$want" | tr ':' '-')" | head -1) || true ;;
    esac
    [ -n "${line:-}" ] || { echo "error: '$want' is not in range (try --list)" >&2; return 1; }

    bssid=$(printf '%s' "$line" | cut -d: -f2 | tr '-' ':')
    chan=$(printf '%s' "$line" | cut -d: -f4)

    case "$bssid" in
        [0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:*) ;;
        *) echo "error: could not read a BSSID out of scan line: $line" >&2; return 1 ;;
    esac
    case "$chan" in
        ''|*[!0-9]*) echo "error: could not read a channel out of scan line: $line" >&2; return 1 ;;
    esac

    printf '%s %s' "$bssid" "$chan"
}

setconf() {
    key=$1; val=$2
    [ -n "$val" ] || { echo "error: refusing to set empty $key" >&2; exit 1; }
    if grep -q "^${key}=" "$CONF"; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$CONF"
    else
        printf '%s=%s\n' "$key" "$val" >> "$CONF"
    fi
}

conf_get() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | tail -1; }

report() {
    # Read the probing back out of the file rather than from the arguments: it may have been
    # set on this run or three weeks ago, and what matters is what the service just started with.
    mode=$(conf_get CSI_PROBE_MODE)
    hz=$(conf_get CSI_PROBE_HZ)
    echo
    echo "measuring $1 on channel $2, probing ${3:-<the gateway>}" \
         "(${mode:-broadcast}) at ${hz:-0} Hz"
    echo "waiting for frames..."
    sleep 12
    line=$(journalctl -u csi-node --since '15 seconds ago' --no-pager 2>/dev/null \
           | sed -n 's/.*INFO \([0-9][0-9]* frames.*\)/\1/p' | tail -1)
    if [ -z "$line" ]; then
        echo "  (no status line yet -- watch it with: journalctl -u csi-node -f)"
        return
    fi
    echo "  $line"
    case "$line" in
        0\ frames*|'')
            echo
            echo "Nothing is arriving. Either that radio is not transmitting, or the probe host"
            echo "is not associated to it -- its replies then come from a different radio and are"
            echo "filtered out. Pick a host on that unit with --probe, or --auto to go back."
            # The wired node's probing is the part of it that has never been measured, so say so
            # rather than letting the reader assume the transmitter choice is what went wrong.
            if [ "$UPLINK" = wired ]; then
                echo
                echo "This node probes from the wire. Broadcast is expected to yield nothing there:"
                echo "an access point puts group-addressed frames on the air at a legacy rate, and"
                echo "the extractor only produces CSI for HT/VHT. What should work is unicast at a"
                echo "host associated to the measured radio, because the access point then has to"
                echo "deliver it over the air as an HT frame -- expected, not verified:"
                echo "    $0 --probe-mode unicast --probe <a wireless client of that radio>"
            fi ;;
    esac
}

[ $# -ge 1 ] || { usage >&2; exit 1; }

case "$1" in
    -h|--help) usage; exit 0 ;;
    --list) list; exit 0 ;;
    --status)
        echo "uplink:      $UPLINK"
        echo "follow mode: $(conf_get CSI_AP_FOLLOW)"
        echo "measuring:   $(sed -n 's/^CSI_AP_MAC=//p' "$RUN_ENV" 2>/dev/null) on $(sed -n 's/^CSI_CHANSPEC=//p' "$RUN_ENV" 2>/dev/null)"
        echo "probing:     $(conf_get CSI_PROBE_HOST) ($(conf_get CSI_PROBE_MODE)) at $(conf_get CSI_PROBE_HZ) Hz"
        echo "service:     $(systemctl is-active csi-node)"
        exit 0 ;;
esac

[ "$(id -u)" = 0 ] || { echo "error: must be run as root" >&2; exit 1; }

# 'keep' is what a run that names no transmitter means: change how the radio is induced and
# leave the choice of radio, and the follow mode that produced it, exactly as they are. Without
# it, adjusting the probe rate would silently re-pin a node that was following its dongle.
MODE=keep
TARGET=''
CHAN=''
PROBE=''
PROBE_HZ=''
PROBE_MODE=''

case "$1" in
    --auto)
        MODE=auto
        shift ;;
    -*)
        ;;
    *)
        resolved=$(resolve "$1")
        TARGET=${resolved%% *}
        CHAN=${resolved##* }
        MODE=manual
        shift ;;
esac

while [ $# -gt 0 ]; do
    case "$1" in
        --probe) [ $# -ge 2 ] || { echo "error: --probe needs a host" >&2; exit 1; }
                 PROBE=$2; shift 2 ;;
        --probe-hz) [ $# -ge 2 ] || { echo "error: --probe-hz needs a number" >&2; exit 1; }
                 PROBE_HZ=$2; shift 2 ;;
        # Validated here rather than left to csi_node.py: an unknown mode there is an argparse
        # error at start, so the service would sit in a restart loop with the reason four
        # journal pages back instead of on the terminal that caused it.
        --probe-mode) [ $# -ge 2 ] || { echo "error: --probe-mode needs broadcast, unicast or icmp" >&2; exit 1; }
                 case "$2" in
                     broadcast|unicast|icmp) ;;
                     *) echo "error: unknown probe mode '$2' (broadcast, unicast, icmp)" >&2; exit 1 ;;
                 esac
                 PROBE_MODE=$2; shift 2 ;;
        *) usage >&2; echo "error: unknown argument '$1'" >&2; exit 1 ;;
    esac
done

if [ "$MODE" = keep ] && [ -z "$PROBE" ] && [ -z "$PROBE_HZ" ] && [ -z "$PROBE_MODE" ]; then
    usage >&2
    echo "error: nothing to change" >&2
    exit 1
fi

# --auto reads the transmitter off the dongle's association, and a wired node has no dongle. Say
# that here: the alternative is a config the node refuses to start with, discovered as a restart
# loop rather than as an answer to the command that caused it.
if [ "$UPLINK" = wired ] && [ "$MODE" = auto ]; then
    echo "error: --auto follows the association on $PROBE_IFACE, and CSI_UPLINK=wired means" >&2
    echo "there is none. Choose a transmitter: $0 --list, then $0 <bssid>." >&2
    exit 1
fi

[ "$MODE" = keep ] || setconf CSI_AP_FOLLOW "$MODE"
if [ "$MODE" = manual ]; then
    setconf CSI_AP_MAC "$TARGET"
    setconf CSI_CHANSPEC "$CHAN/20"
fi
[ -n "$PROBE" ] && setconf CSI_PROBE_HOST "$PROBE"
[ -n "$PROBE_HZ" ] && setconf CSI_PROBE_HZ "$PROBE_HZ"
[ -n "$PROBE_MODE" ] && setconf CSI_PROBE_MODE "$PROBE_MODE"

# Warn before the restart, not after: on a busy access point the household's own traffic keeps
# the rate up, so a broadcast probe from the wire contributing nothing at all still reads as a
# healthy node. The measurement that says this: the extractor produces CSI only for HT/VHT
# frames, and group-addressed traffic leaves an access point at a legacy rate.
if [ "$UPLINK" = wired ]; then
    eff_mode=${PROBE_MODE:-$(conf_get CSI_PROBE_MODE)}
    eff_hz=${PROBE_HZ:-$(conf_get CSI_PROBE_HZ)}
    if [ "${eff_mode:-broadcast}" = broadcast ] && [ "${eff_hz:-0}" != 0 ]; then
        echo "warning: probing in broadcast mode from a wired uplink is expected to induce" \
             "nothing measurable; try --probe-mode unicast --probe <a wireless client of the" \
             "measured radio>" >&2
    fi
fi

systemctl restart csi-node
sleep 2

if [ "$MODE" = manual ]; then
    report "$TARGET" "$CHAN" "$(conf_get CSI_PROBE_HOST)"
else
    # auto and keep both mean the answer is whatever capture-up.sh just derived or confirmed,
    # which it has written to $RUN_ENV.
    report "$(sed -n 's/^CSI_AP_MAC=//p' "$RUN_ENV" 2>/dev/null)" \
           "$(sed -n 's/^CSI_CHANSPEC=//p' "$RUN_ENV" 2>/dev/null)" \
           "$(conf_get CSI_PROBE_HOST)"
fi
