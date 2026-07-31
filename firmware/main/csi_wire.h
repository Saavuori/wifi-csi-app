// Uplink wire format v2. Mirrored in server/csi/protocol.py and web/src/lib/protocol.ts;
// see docs/wire-format.md. server/tests/test_protocol.py pins the byte layout.
//
// Little-endian throughout, which is free on the ESP32 and on every machine that will ever
// parse this.
//
// v2 appends src_mac, link_epoch and a reserved byte to v1's 22 bytes. Everything at offsets
// 0..21 is unchanged, so a v1 recording still parses — which matters, because "a recording
// replays byte-identically" is a property the whole design leans on.

#pragma once

#include <stdint.h>

#define CSI_WIRE_MAGIC 0x4353  // 'S','C' on the wire, little-endian
#define CSI_WIRE_VERSION 2

#define CSI_SEC_CHANNEL_NONE 0
#define CSI_SEC_CHANNEL_ABOVE 1
#define CSI_SEC_CHANNEL_BELOW 2

// Packed, because this struct *is* the wire. Without the attribute the compiler inserts
// padding after `version` to align `seq`, and the server would read four bytes of garbage
// into the sequence number — a failure that looks like massive packet loss rather than like
// a struct layout bug.
typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t version;
    uint8_t node_id;
    uint32_t seq;
    uint64_t timestamp;  // esp_timer_get_time(), microseconds since boot
    int8_t rssi;
    int8_t noise_floor;
    uint8_t channel;
    uint8_t sec_channel;
    uint16_t n_sub;  // subcarriers in THIS frame; never assume 64
    // Who transmitted the frame this measurement came from. In the station role that is the
    // access point's BSSID, and it is the far end of the link being sensed; in the receiver
    // role it is the transmitter node's MAC. Same six bytes either way, and worth carrying:
    // it lands in the recording, so which AP a session was measured through is answerable
    // afterwards rather than only while the node is in front of you.
    uint8_t src_mac[6];
    // Increments on every association. The server compares it against the previous frame's,
    // and a change means roam, reconnect, or channel change — the link is not the link it was,
    // and every baseline built on the old one has to go.
    uint8_t link_epoch;
    uint8_t reserved;
    // int8_t data[2 * n_sub] follows: interleaved (imag, real), exactly as esp_wifi hands it
    // over. No byte shuffling in the callback.
} csi_wire_header_t;

_Static_assert(sizeof(csi_wire_header_t) == 30, "wire header must be 30 bytes with no padding");
