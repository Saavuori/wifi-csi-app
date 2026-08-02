#!/bin/sh
# Publish the radios in range, and carry out selections asked for from the web UI.
#
# The server runs in a container and the things a selection needs -- nmcli, systemctl, the
# nexmon tools -- are all on the host, so the two talk through the data directory that is
# already bind-mounted into the container. Nothing here grants the container any privilege: it
# writes a request naming a BSSID, and this agent decides what to do about it.
#
# The one rule that matters: a selection must never touch the association on CSI_PROBE_IFACE.
# That dongle is the only route into this machine, and capture is independent of it -- the
# monitor radio can sit on any channel regardless of where the dongle is associated. So a
# request can move what we measure, and can never cost us the machine.
#
# Where the list comes from depends on how the node is reached. With a dongle there is a managed
# Wi-Fi interface and nmcli produces the list for free. With CSI_UPLINK=wired there is no station
# on this machine at all, so the list has to come out of the capture radio itself: see
# csi-scan-monitor.sh. That path costs measurement -- a sweep stops csi-node for about half a
# minute -- so it runs on a long interval and on demand, never on the nmcli path's timer.
set -eu

CONF=/etc/default/csi-node
. "$CONF"

# The host directory the server container has bind-mounted as /data. install-node.sh writes it
# into $CONF; there is no default worth guessing, because publishing aps.json into a directory
# nothing reads presents as a picker that never appears, with no error anywhere to explain it.
DATA="${CSI_DATA_DIR:?set CSI_DATA_DIR in /etc/default/csi-node to the host directory the server container mounts as /data}"
APS="$DATA/aps.json"
REQ="$DATA/ap-select.request.json"
RESULT="$DATA/ap-select.result.json"
# Where csi_node.py's ProbeAudit leaves its self-measurement, alongside /run/csi-node.env. It is
# the only evidence that provoking traffic is doing anything, which on a wired backbone is the
# open question of the whole mode, so it is published rather than left in the journal.
AUDIT="${CSI_AUDIT_FILE:-/run/csi-node.audit}"
INTERVAL="${CSI_AP_SCAN_INTERVAL:-60}"
PROBE_IFACE="${CSI_PROBE_IFACE:-wlan1}"

# Only the exact value 'wired' switches the list over to the monitor sweep. Anything else,
# including the variable being absent on an installation that predates it, keeps the nmcli path:
# a node that does have a dongle must not lose its picker to a typo in the config.
# Default it here rather than publishing an empty string. An install predating the wired mode has
# no CSI_UPLINK at all, and every consumer of this field would then have to guess what nothing
# means — the UI would show a node with no uplink, which is not a state that exists.
UPLINK="${CSI_UPLINK:-dongle}"
SELF_DIR=$(dirname "$0")
SCAN_SH="${CSI_SCAN_SH:-$SELF_DIR/csi-scan-monitor.sh}"
# The last sweep's list, in /run rather than in $DATA: the container has no use for it, and after
# a reboot the honest answer is that nothing has been swept yet.
SCAN_CACHE=/run/csi-node-aps.scan
# Long, because each sweep is ~30 s with no measurement coming out. 0 turns the timer off and
# leaves the list to the sweep at startup and whatever a request asks for.
SWEEP_INTERVAL="${CSI_AP_SWEEP_INTERVAL:-900}"
case "$SWEEP_INTERVAL" in
    ''|*[!0-9]*)
        echo "warning: CSI_AP_SWEEP_INTERVAL='$SWEEP_INTERVAL' is not a number of seconds;" \
             "using 900" >&2
        SWEEP_INTERVAL=900 ;;
esac

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

# Control characters are not legal inside a JSON string, and this escapes text that came from
# another program's stderr. A tab in a csi-select-ap error message used to produce a result file
# that no strict parser would read, which the server reported as "pending" forever and the UI as
# a host that never answered -- a failed selection presenting as a hang.
json_escape() {
    printf '%s' "$1" | tr '\011' ' ' | tr -d '\000-\010\013\014\016-\037' \
        | sed 's/\\/\\\\/g; s/"/\\"/g'
}

wired() { [ "$UPLINK" = wired ]; }

publish() {
    measured_mac=$(sed -n 's/^CSI_AP_MAC=//p' /run/csi-node.env 2>/dev/null || true)
    measured_spec=$(sed -n 's/^CSI_CHANSPEC=//p' /run/csi-node.env 2>/dev/null || true)
    follow=$(sed -n 's/^CSI_AP_FOLLOW=//p' "$CONF" 2>/dev/null || true)
    probe_host=$(sed -n 's/^CSI_PROBE_HOST=//p' "$CONF" 2>/dev/null || true)
    probe_hz=$(sed -n 's/^CSI_PROBE_HZ=//p' "$CONF" 2>/dev/null || true)
    probe_mode=$(sed -n 's/^CSI_PROBE_MODE=//p' "$CONF" 2>/dev/null || true)

    tmp="$APS.tmp"
    {
        printf '{"updated_at":%s,' "$(date +%s)"
        printf '"follow":"%s",' "$(json_escape "${follow:-auto}")"
        printf '"measured":{"bssid":"%s","chanspec":"%s"},' \
            "$(json_escape "$measured_mac")" "$(json_escape "$measured_spec")"
        printf '"uplink":"%s",' "$(json_escape "$UPLINK")"
        printf '"probe":{"host":"%s","hz":%s,"mode":"%s"' \
            "$(json_escape "$probe_host")" "${probe_hz:-0}" \
            "$(json_escape "${probe_mode:-broadcast}")"
        # Whatever the node last measured about its own probing, if it has measured anything.
        # Written by ProbeAudit; absent until the first audit completes, and absent for good if
        # auditing is switched off -- which is why every one of these is optional downstream.
        if [ -r "$AUDIT" ]; then
            induced=$(sed -n 's/^induced_hz=//p' "$AUDIT" 2>/dev/null | head -1)
            total=$(sed -n 's/^total_hz=//p' "$AUDIT" 2>/dev/null | head -1)
            background=$(sed -n 's/^background_hz=//p' "$AUDIT" 2>/dev/null | head -1)
            [ -n "$induced" ] && printf ',"induced_hz":%s' "$induced"
            [ -n "$total" ] && printf ',"total_hz":%s' "$total"
            [ -n "$background" ] && printf ',"background_hz":%s' "$background"
        fi
        printf '},'
        if wired; then
            # Both extra keys, so the shape the server and the web UI already read is unchanged.
            # They are here because an empty ssid on every entry otherwise looks like a bug in
            # the scan rather than the only possible answer, and because 'updated_at' is when
            # this file was written, which in wired mode can be a quarter of an hour newer than
            # the list inside it.
            printf '"note":"%s",' "$(json_escape "$WIRED_NOTE")"
            printf '"scanned_at":%s,' "$(scanned_at)"
        fi
        printf '"aps":'
        if wired; then swept_aps; else nmcli_aps; fi
        printf '}'
    } > "$tmp"
    # Rename rather than write in place: the server may read this at any moment and half a
    # JSON document is worse than a stale one.
    mv -f "$tmp" "$APS"
    chmod 0644 "$APS"
}

nmcli_aps() {
    printf '['
    first=1
    # nmcli escapes the colons inside a BSSID as '\:'; turn those into '-' so the remaining
    # colons separate fields, and back again per value.
    #
    # 'auto' lets NetworkManager reuse its own results and only go looking when they are
    # stale. Scanning costs the dongle a second or two off channel and that dongle is the
    # only route into this machine, but NetworkManager scans on its own schedule anyway and
    # an association survives it. 'no' was too cold: its cache is often nearly empty, and a
    # picker that lists one radio is not a picker.
    nmcli -t -f IN-USE,BSSID,SSID,CHAN,SIGNAL dev wifi list ifname "$PROBE_IFACE" \
        --rescan auto 2>/dev/null | sed 's/\\:/-/g' | while IFS=: read -r inuse bssid ssid chan signal; do
        [ -n "$bssid" ] || continue
        mac=$(printf '%s' "$bssid" | tr '-' ':')
        [ "$first" = 1 ] || printf ','
        first=0
        printf '{"bssid":"%s","ssid":"%s","channel":%s,"signal":%s,"in_use":%s}' \
            "$(json_escape "$mac")" "$(json_escape "$ssid")" \
            "${chan:-0}" "${signal:-0}" \
            "$([ "$inuse" = '*' ] && echo true || echo false)"
    done
    printf ']'
}

# -- the wired path: no station, so the list comes from the capture radio ----------------------

WIRED_NOTE="from a monitor-mode sweep of the capture radio: BSSIDs only, because beacons produce no CSI and a network name travels nowhere else, and the entries are transmitters heard, which includes client stations as well as access points"

# The list is served from the last sweep rather than swept on demand, because publish() is also
# what runs after a selection and on the ordinary minute timer, and neither of those is worth
# half a minute of dead capture. An empty list is what a node that has not swept yet honestly
# has; the sweep at startup fills it within the first minute.
swept_aps() {
    if [ -s "$SCAN_CACHE" ]; then
        cat "$SCAN_CACHE"
    else
        printf '[]'
    fi
}

# When the list inside the file was actually measured, as opposed to when the file was written.
# Anything but a number becomes 0: this goes into JSON unquoted, and an empty one would leave the
# server parsing a document that ends mid-object.
scanned_at() {
    at=$(stat -c %Y "$SCAN_CACHE" 2>/dev/null || true)
    case "$at" in
        ''|*[!0-9]*) at=0 ;;
    esac
    printf '%s' "$at"
}

# Replaces the cached list, or leaves the previous one alone. A sweep that failed must not empty
# the picker: the radios did not go anywhere, and a list that is twenty minutes old is worth more
# than no list at all. Everything the sweep says about itself goes to the journal.
sweep() {
    if ! wired; then
        return 1
    fi
    if [ ! -x "$SCAN_SH" ]; then
        echo "error: $SCAN_SH is missing or not executable, so CSI_UPLINK=wired has no way to" \
             "discover anything -- the picker will stay empty" >&2
        return 1
    fi
    tmp="$SCAN_CACHE.tmp"
    if ! "$SCAN_SH" --format aps > "$tmp"; then
        echo "warning: the monitor sweep failed; keeping the previous list" >&2
        rm -f "$tmp"
        return 1
    fi
    case "$(head -c 1 "$tmp" 2>/dev/null)" in
        '[') mv -f "$tmp" "$SCAN_CACHE" ;;
        *) echo "warning: the monitor sweep produced no JSON; keeping the previous list" >&2
           rm -f "$tmp"
           return 1 ;;
    esac
    return 0
}

apply() {
    id=$(sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQ" | head -1)
    mode=$(sed -n 's/.*"mode"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQ" | head -1)
    bssid=$(sed -n 's/.*"bssid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQ" | head -1)
    probe=$(sed -n 's/.*"probe_host"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQ" | head -1)
    probe_mode=$(sed -n 's/.*"probe_mode"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQ" | head -1)
    probe_hz=$(sed -n 's/.*"probe_hz"[[:space:]]*:[[:space:]]*\([0-9.]*\).*/\1/p' "$REQ" | head -1)
    rescan=$(sed -n 's/.*"rescan"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p' "$REQ" | head -1)
    rm -f "$REQ"

    # A rescan on its own. Only the wired path has anything to do here: with a dongle the list is
    # rebuilt from nmcli by publish() on every timer tick anyway, so answering 'done' is honest.
    if [ "$mode" = "rescan" ] || [ "$mode" = "scan" ]; then
        if ! wired; then
            publish
            write_result "$id" true ""
            return
        fi
        if sweep; then
            publish
            write_result "$id" true ""
        else
            publish
            write_result "$id" false "the monitor sweep failed; the previous list is unchanged"
        fi
        return
    fi

    # A selection that also asks for a fresh list. Sweeping first means the BSSID being selected
    # was seen recently, and it happens before the capture is moved rather than after, so the
    # half minute of dead capture is spent while the node is being reconfigured anyway.
    if [ "$rescan" = "true" ] && wired; then
        sweep || true
    fi

    set --
    if [ "$mode" = "auto" ]; then
        set -- --auto
    else
        case "$bssid" in
            [0-9a-fA-F][0-9a-fA-F]:*) set -- "$bssid" ;;
            *) write_result "$id" false "not a BSSID: $bssid"; return ;;
        esac
    fi
    [ -n "$probe" ] && set -- "$@" --probe "$probe"
    # How traffic is provoked is part of the same choice as which radio to measure, and on a
    # wired backbone it is the more consequential half: without an association there is no
    # acknowledgement to collect, so broadcast may buy nothing and unicast at one of that radio's
    # clients is the only thing that can. Validated here rather than trusted, because this comes
    # from the container.
    case "${probe_mode:-}" in
        "") ;;
        broadcast|unicast|icmp) set -- "$@" --probe-mode "$probe_mode" ;;
        *) write_result "$id" false "not a probe mode: $probe_mode"; return ;;
    esac
    case "${probe_hz:-}" in
        "") ;;
        *[!0-9.]*|.*.*) write_result "$id" false "not a probe rate: $probe_hz"; return ;;
        *) set -- "$@" --probe-hz "$probe_hz" ;;
    esac

    if err=$(csi-select-ap "$@" 2>&1); then
        write_result "$id" true ""
    else
        write_result "$id" false "$(printf '%s' "$err" | tail -3 | tr '\n' ' ')"
    fi
    publish
}

write_result() {
    printf '{"id":"%s","ok":%s,"error":"%s","finished_at":%s}\n' \
        "$(json_escape "$1")" "$2" "$(json_escape "$3")" "$(date +%s)" > "$RESULT.tmp"
    mv -f "$RESULT.tmp" "$RESULT"
    chmod 0644 "$RESULT"
}

[ "$(id -u)" = 0 ] || { echo "error: must run as root" >&2; exit 1; }
mkdir -p "$DATA"

# Publish before sweeping, so that a UI opened during the first half minute sees the node rather
# than nothing at all -- with an empty list and a note saying where the list comes from.
publish
if wired; then
    if sweep; then publish; fi
fi

waited=0
swept=0
while :; do
    if [ -f "$REQ" ]; then
        apply
        waited=0
    fi
    sleep 1
    waited=$((waited + 1))
    swept=$((swept + 1))
    if [ "$waited" -ge "$INTERVAL" ]; then
        # Cheap on both paths: nmcli reuses NetworkManager's own results, and the wired path
        # reads the cached sweep. What this refreshes every minute is what is being measured.
        publish
        waited=0
    fi
    # The expensive one. A sweep stops csi-node for about half a minute, so it runs on its own
    # much longer interval -- and while it runs, a selection request waits, which is the honest
    # trade: the radio cannot be swept and measured at the same time.
    if wired && [ "$SWEEP_INTERVAL" -gt 0 ] && [ "$swept" -ge "$SWEEP_INTERVAL" ]; then
        if sweep; then publish; fi
        swept=0
    fi
done
