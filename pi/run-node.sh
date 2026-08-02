#!/bin/sh
# Start csi_node.py with the configured environment plus whatever capture-up.sh derived from
# the dongle's live association. /run/csi-node.env is written by capture-up.sh moments earlier
# and overrides the static config, so the software transmitter filter always matches the
# firmware filter -- if those two disagree every frame is discarded and the node reports zero.
set -eu
set -a
. /etc/default/csi-node
[ -r /run/csi-node.env ] && . /run/csi-node.env
set +a
exec /usr/bin/python3 /opt/csi-node/bin/csi_node.py "$@"
