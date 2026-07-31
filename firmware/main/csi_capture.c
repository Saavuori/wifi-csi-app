// CSI capture: configure the radio, and get out of the way.
//
// The rule this file exists to obey: the callback runs in the WiFi driver task on Core 0, and
// it does nothing but build a header, memcpy the payload, and return. No sending, no logging,
// no allocation, no blocking primitive with any timeout. Everything else happens on Core 1.

#include "csi_capture.h"

#include <string.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "csi_settings.h"
#include "csi_wire.h"

static const char *TAG = "csi.capture";

// The ring's slot size and the wire format are set in two different headers, and this is the one
// translation unit that sees both. A slot that cannot hold the largest frame does not corrupt
// anything — the size check below rejects it — but it drops every wide frame while every counter
// except `oversize` reads normal, which is a much harder failure to find than a build error.
_Static_assert(CSI_SLOT_BYTES >= CSI_WIRE_MAX_FRAME_BYTES,
               "a ring slot must hold the largest frame this firmware can emit");

static csi_ring_t *s_ring;
static csi_capture_stats_t s_stats;
static uint32_t s_seq;
static volatile bool s_filter_enabled;
static uint8_t s_peer[6];
static volatile uint8_t s_link_epoch;
static TaskHandle_t s_consumer;
// Cached at start rather than read through csi_settings() per frame. The callback is the one
// place in this firmware where a pointer chase is worth avoiding on principle.
static uint8_t s_node_id;

// A scratch buffer owned by the callback. It is only ever touched from the WiFi task, so it
// needs no protection, and it saves building the frame on the stack of a task whose stack size
// we do not control.
static uint8_t s_scratch[CSI_SLOT_BYTES];

void csi_capture_set_consumer(TaskHandle_t task) { s_consumer = task; }

void csi_capture_set_peer(const uint8_t mac[6]) {
    if (mac == NULL) {
        s_filter_enabled = false;
        return;
    }
    // Disable, copy, re-enable. This runs from the event task while the callback may be reading
    // s_peer, and the ordering decides what a race costs: with the flag cleared first, a frame
    // arriving mid-copy is accepted rather than compared against half of one MAC and half of
    // another. Accepting one frame too many is harmless; rejecting every frame after a roam
    // because the comparison tore is not.
    s_filter_enabled = false;
    memcpy(s_peer, mac, 6);
    s_filter_enabled = true;
    ESP_LOGI(TAG, "filtering CSI to %02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2],
             mac[3], mac[4], mac[5]);
}

void csi_capture_set_link_epoch(uint8_t epoch) { s_link_epoch = epoch; }

static void IRAM_ATTR csi_callback(void *ctx, wifi_csi_info_t *info) {
    (void)ctx;
    if (info == NULL || info->buf == NULL || info->len < 2) {
        return;
    }

    if (s_filter_enabled && memcmp(info->mac, s_peer, 6) != 0) {
        s_stats.filtered++;
        return;
    }

    const uint16_t n_sub = info->len / 2;
    const size_t total = sizeof(csi_wire_header_t) + (size_t)info->len;
    if (total > CSI_SLOT_BYTES) {
        s_stats.oversize++;
        return;
    }

    csi_wire_header_t *header = (csi_wire_header_t *)s_scratch;
    header->magic = CSI_WIRE_MAGIC;
    header->version = CSI_WIRE_VERSION;
    header->node_id = s_node_id;
    // Sequence advances once per frame we accept, whether or not the ring has room. That is
    // deliberate: a frame dropped on the device then shows up at the server as a gap, which is
    // exactly what the loss statistic should be counting.
    header->seq = s_seq++;
    // Timestamped here, on the device, in microseconds. Never infer timing from server arrival
    // time — the analysis is frequency-domain and network jitter would destroy it.
    header->timestamp = (uint64_t)esp_timer_get_time();
    header->rssi = info->rx_ctrl.rssi;
    header->noise_floor = info->rx_ctrl.noise_floor;
    header->channel = info->rx_ctrl.channel;
    header->sec_channel = info->rx_ctrl.secondary_channel;
    header->n_sub = n_sub;
    memcpy(header->src_mac, info->mac, 6);
    header->link_epoch = s_link_epoch;
    header->reserved = 0;

    memcpy(s_scratch + sizeof(csi_wire_header_t), info->buf, info->len);

    if (csi_ring_push(s_ring, s_scratch, total)) {
        s_stats.frames++;
        if (s_consumer != NULL) {
            // Cheap, non-blocking, and safe from a task context. Waking the packer here rather
            // than letting it poll keeps latency low without a busy loop.
            xTaskNotifyGive(s_consumer);
        }
    } else {
        s_stats.dropped++;
    }
}

esp_err_t csi_capture_start(csi_ring_t *ring) {
    s_ring = ring;
    memset(&s_stats, 0, sizeof(s_stats));
    s_seq = 0;
    s_node_id = csi_settings()->node_id;

    // Fields below are the ESP32/S3 CSI configuration (IDF 5.x). Legacy long training field
    // only: it gives 64 subcarriers in HT20 and is present on every frame, where HT-LTF only
    // appears on 802.11n frames. Mixing the two would make n_sub jump between 64 and 128
    // depending on what the transmitter happened to send.
    wifi_csi_config_t config = {
        .lltf_en = true,
        .htltf_en = false,
        .stbc_htltf2_en = false,
        .ltf_merge_en = false,
        // Filter to the primary channel. Without this, energy from the secondary channel leaks
        // into the estimate and the subcarriers at the band edges become unusable.
        .channel_filter_en = true,
        // Let the hardware scale, and report the shift. Manual scaling requires knowing the
        // right constant for the board and the AGC state, which is exactly the thing we cannot
        // know — the server removes the gain instead, using RSSI.
        .manu_scale = false,
        .shift = 0,
    };

    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_callback, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "CSI capture enabled (node %u)", (unsigned)s_node_id);
    return ESP_OK;
}

void csi_capture_stop(void) {
    esp_wifi_set_csi(false);
    esp_wifi_set_csi_rx_cb(NULL, NULL);
}

void csi_capture_get_stats(csi_capture_stats_t *out) {
    *out = s_stats;
    out->dropped = csi_ring_dropped(s_ring);
}
