"""WiFi CSI sensing: ingest, record, replay, analyse.

The pipeline is source-agnostic by design. A frame is (timestamp_us, node_id, complex[N]) with
N variable. An ESP32, a future Raspberry Pi running Nexmon, and a replayed public dataset are
all just producers into that format, and nothing downstream knows or cares which it is looking
at.

The version is not here: it is the release tag, resolved at runtime by `csi.version`.
"""
