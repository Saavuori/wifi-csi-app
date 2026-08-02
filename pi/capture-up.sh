#!/bin/sh
# Configure the nexmon extractor for monitor-mode capture on the onboard radio.
#
# Monitor mode, not associated mode: this firmware destroys inbound unicast reception while the
# extractor is installed -- the ucode deafens the receiver for the duration of every CSI dump --
# so the radio cannot hold a usable link and does nothing but listen. The association and the
# probe traffic live on the USB dongle instead; see /etc/default/csi-node.
#
# What we measure is whatever the dongle is talking to, so the transmitter and the channel are
# read from the dongle's live association rather than configured. That matters because the
# dongle is also the only way into this machine: its profile must stay free to associate
# wherever it can, and pinning capture to a hardcoded BSSID would silently produce zero frames
# the first time it landed somewhere else. The derived values are written to /run/csi-node.env
# so csi_node.py's software filter agrees with the firmware's.
#
# CSI_UPLINK=wired is the other shape of node: Ethernet carries the network, the onboard radio
# still captures, and there is no dongle at all. It is the safer arrangement -- no capture change
# can cost the route into the machine -- but it removes the association this script derives its
# answer from, so on a wired node the transmitter has to be chosen by hand.
set -eu
. /etc/default/csi-node

IFACE="${CSI_IFACE:-wlan0}"
PROBE_IFACE="${CSI_PROBE_IFACE:-wlan1}"
# Which interface carries the network, and therefore whether there is anything to follow.
# Defaulting to 'dongle' means an existing install that has never heard of this variable keeps
# behaving exactly as it did.
UPLINK="${CSI_UPLINK:-dongle}"
RUN_ENV=/run/csi-node.env

# -- pick the transmitter ----------------------------------------------------------------

# On a wired node nothing is associated, so CSI_AP_FOLLOW=auto has nothing to mean: there is no
# BSSID to read and no channel to copy. Refusing to start is the point. An unconfigured capture
# still arms perfectly happily -- it just listens to a channel nobody asked for and reports a
# rate of zero, which is indistinguishable from an empty room, so the failure would only be
# noticed hours later in data that was never going to contain anything.
if [ "$UPLINK" = "wired" ]; then
    # A chanspec is "<channel>/<width>", so anything that does not start with a digit is either
    # empty or the leftovers of a half-written selection.
    case "${CSI_CHANSPEC:-}" in
        [0-9]*) chanspec_ok=1 ;;
        *)      chanspec_ok=0 ;;
    esac

    if [ -z "${CSI_AP_MAC:-}" ] || [ "$chanspec_ok" = 0 ]; then
        echo "error: CSI_UPLINK=wired, so there is no association to derive a transmitter from," \
             "and none is configured (CSI_AP_MAC='${CSI_AP_MAC:-}' CSI_CHANSPEC='${CSI_CHANSPEC:-}')" >&2
        echo "Choose one -- on a wired node the list comes from the monitor radio's own sweep," >&2
        echo "which csi-ap-agent publishes, rather than from a scan:" >&2
        echo "    csi-select-ap --list" >&2
        echo "    csi-select-ap <bssid> --probe-mode unicast --probe <a wireless client of it>" >&2
        echo "Unicast, because an access point sends broadcast traffic at a legacy rate and the" >&2
        echo "extractor only dumps CSI for HT/VHT frames; what it must put on the air in a form" >&2
        echo "we can measure is a frame addressed to one of its own stations." >&2
        exit 1
    fi

    # Not fatal, because the configured pair is complete and capturing it is what was asked for.
    # Worth saying once per start: the config claims a mode that cannot happen here, which
    # usually means the file was copied from a node that has a dongle.
    if [ "${CSI_AP_FOLLOW:-auto}" != "manual" ]; then
        echo "warning: CSI_AP_FOLLOW=${CSI_AP_FOLLOW:-auto} is ignored on a wired node -- there is" \
             "no association to follow; using the configured radio as if it were 'manual'" >&2
    fi

    echo "wired uplink: measuring the configured radio: $CSI_AP_MAC on $CSI_CHANSPEC"
# CSI_AP_FOLLOW=manual means someone chose a radio with csi-select-ap.sh and the configured
# BSSID and channel are the answer -- possibly a unit the dongle is not associated to at all,
# which is the usual case when the interesting path runs through another room. Only in auto
# mode do we read the choice off the dongle's own association.
elif [ "${CSI_AP_FOLLOW:-auto}" = "manual" ] && [ -n "${CSI_AP_MAC:-}" ] && [ -n "${CSI_CHANSPEC#/}" ]; then
    echo "measuring the configured radio: $CSI_AP_MAC on $CSI_CHANSPEC"
else
    # Falling back keeps the node producing data, which is the right call at boot -- but if a
    # radio was chosen by hand and the choice is unusable, silence would mean measuring a
    # different room than the one that was asked for.
    if [ "${CSI_AP_FOLLOW:-auto}" = "manual" ]; then
        echo "warning: CSI_AP_FOLLOW=manual but the chosen radio is incomplete" \
             "(CSI_AP_MAC='${CSI_AP_MAC:-}' CSI_CHANSPEC='${CSI_CHANSPEC:-}');" \
             "following $PROBE_IFACE instead -- re-run csi-select-ap" >&2
    fi

# -- follow the dongle -------------------------------------------------------------------

# "Connected to xx:xx:xx:xx:xx:xx (on wlan1)"
bssid=$(iw dev "$PROBE_IFACE" link 2>/dev/null | sed -n 's/^Connected to \([0-9a-fA-F:]\{17\}\).*/\1/p')
# "channel 9 (2452 MHz), width: 40 MHz, center1: 2462 MHz"
chan_line=$(iw dev "$PROBE_IFACE" info 2>/dev/null | sed -n 's/^[[:space:]]*channel /channel /p')
channel=$(printf '%s' "$chan_line" | sed -n 's/^channel \([0-9]\{1,3\}\).*/\1/p')

# Always capture 20 MHz on the control channel, even when the access point runs HT40.
#
# Not a simplification -- the wider forms do not describe this AP's block. makecsiparams takes
# 2.4 GHz 40 MHz only as "<control>u", which encodes the block *below* the control channel
# (measured: "9u" -> chanspec 0x1907, centred on channel 7), while this AP's HT40 sits above it
# (control 9, center1 2462 = channel 11). The matching "9l" is accepted but silently degrades to
# a 20 MHz chanspec. So the only 40 MHz capture available here would spend half its bandwidth on
# spectrum the transmitter is not using.
#
# Capturing the control channel at 20 MHz is what any 20 MHz receiver sees of those frames, and
# it yields 64 subcarriers -- the exact layout the ESP32 nodes produce and the DSP was tuned on.
if [ -n "$bssid" ] && [ -n "$channel" ]; then
    CSI_AP_MAC="$bssid"
    CSI_CHANSPEC="$channel/20"
    echo "following $PROBE_IFACE: $CSI_AP_MAC on $CSI_CHANSPEC"
else
    # Not associated yet, or iw changed its output. Fall back to whatever is configured and say
    # so -- a wrong channel here is invisible in the data, it just reads as a quiet room.
    echo "warning: could not read $PROBE_IFACE association; falling back to configured" \
         "${CSI_AP_MAC:-<none>} on ${CSI_CHANSPEC:-<none>}" >&2
fi

fi

: > "$RUN_ENV"
chmod 0644 "$RUN_ENV"
printf 'CSI_AP_MAC=%s\nCSI_CHANSPEC=%s\n' "${CSI_AP_MAC:-}" "${CSI_CHANSPEC:-}" >> "$RUN_ENV"

[ -n "${CSI_CHANSPEC:-}" ] || { echo "error: no chanspec to capture on" >&2; exit 1; }

# -- arm the extractor -------------------------------------------------------------------

# NetworkManager is kept off this interface permanently by
# /etc/NetworkManager/conf.d/99-csi-node.conf. This is belt and braces for a hand-run.
nmcli dev set "$IFACE" managed no 2>/dev/null || true
ip link set "$IFACE" up 2>/dev/null || true

# Monitor first: -m1 resets the extractor configuration, so setting the params before it
# silently loses them and you capture the whole channel with no filter.
nexutil -I"$IFACE" -m1 >/dev/null 2>&1 || true
sleep 1

# -m restricts dumps to one transmitter in the firmware, and it is enforced on this build. That
# matters for more than tidiness: every dump deafens the receiver, so dumping on unrelated
# traffic costs frames we wanted. Measured on channel 9, adding it took the node from 43 Hz to
# 52 Hz and foreign frames from ~45 Hz to zero.
set -- -c "$CSI_CHANSPEC" -C 0x1 -N 0x1
[ -n "${CSI_AP_MAC:-}" ] && set -- "$@" -m "$CSI_AP_MAC"

PARAMS=$(makecsiparams "$@")
LEN=$(makecsiparams "$@" -r | wc -c)
nexutil -I"$IFACE" -s500 -b -l"$LEN" -v"$PARAMS" >/dev/null

iw dev "$IFACE" set power_save off 2>/dev/null || true
nexutil -I"$IFACE" -s86 -i -v0 >/dev/null 2>&1 || true

# The arm is not trustworthy on its own: nexutil reports transport failures on stderr and still
# exits 0, so read csi_collect back out of firmware shared memory and fail loudly if it did not
# take. Byte 0 of the ioctl 501 hexdump is the flag.
collect=$(nexutil -I"$IFACE" -g501 2>/dev/null | sed -n 's/^0x000000: *\([0-9a-f][0-9a-f]\).*/\1/p')
if [ "$collect" != "01" ]; then
    echo "error: extractor did not arm (csi_collect=${collect:-unreadable})" >&2
    echo "check that nexutil is the USE_VENDOR_CMD build and that $IFACE runs the CSI firmware" >&2
    exit 1
fi

echo "monitor mode on $IFACE, chanspec $CSI_CHANSPEC, keeping ${CSI_AP_MAC:-<all transmitters>}"
