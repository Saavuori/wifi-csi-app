// ESP32-S3 CSI node.
//
// Two roles, chosen in menuconfig:
//
//   RECEIVER     joins the access point for connectivity, then listens promiscuously and
//                reports CSI for frames from the transmitter node to the server over UDP.
//   TRANSMITTER  joins the same access point and emits a small datagram at a fixed rate, so
//                the receiver has something steady to measure.
//
// The pair is the plan's topology B, and the reason for it is sampling rate: with a single
// node harvesting CSI from whatever your router happens to send, you do not control the rate
// and the spectrum you compute is a spectrum of your router's traffic pattern. Set
// CSI_ROLE_RECEIVER on one board and CSI_ROLE_TRANSMITTER on the other.
//
// Topology A from the plan — one node in station mode, pinging the gateway — is also
// supported: build a single receiver with CSI_PEER_MAC set to your router's BSSID.

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "csi_capture.h"
#include "csi_net.h"
#include "csi_ring.h"
#include "csi_tx.h"
#include "csi_wifi.h"

static const char *TAG = "csi";

// Statically allocated. About 26 KB at the default slot count — worth it to have the ring exist
// before the WiFi driver does, and to make it impossible for a CSI callback to arrive while
// the buffer it writes into is still being allocated.
static csi_ring_t s_ring;

static void report_task(void *arg) {
    (void)arg;
    csi_capture_stats_t capture_prev = {0};
    csi_net_stats_t net_prev = {0};
    const int period_s = CONFIG_CSI_REPORT_PERIOD_S;

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(period_s * 1000));

        csi_capture_stats_t capture;
        csi_net_stats_t net;
        csi_capture_get_stats(&capture);
        csi_net_get_stats(&net);

        const uint32_t frames = capture.frames - capture_prev.frames;
        const uint32_t dropped = capture.dropped - capture_prev.dropped;
        const uint32_t sent = net.sent - net_prev.sent;

        // The numbers Phase 1's exit criteria are written against: a stable rate, drops under
        // 1%, and a queue depth that is not creeping upwards. A queue that grows means the
        // sender is losing the race with the callback, which is the first sign that something
        // is blocking on Core 1.
        ESP_LOGI(TAG,
                 "%.1f Hz | sent %u | ring drop %u | filtered %u | oversize %u | queued %u | "
                 "send errors %u | heap %u",
                 (float)frames / period_s, (unsigned)sent, (unsigned)dropped,
                 (unsigned)capture.filtered, (unsigned)capture.oversize, (unsigned)net.queued,
                 (unsigned)net.send_errors, (unsigned)esp_get_free_heap_size());

        capture_prev = capture;
        net_prev = net;
    }
}

void app_main(void) {
    csi_ring_init(&s_ring);

    ESP_LOGI(TAG, "node %d starting", CONFIG_CSI_NODE_ID);
    ESP_ERROR_CHECK(csi_wifi_start_sta());

#if CONFIG_CSI_ROLE_TRANSMITTER
    ESP_LOGI(TAG, "role: transmitter");
    ESP_ERROR_CHECK(csi_tx_start());
#else
    ESP_LOGI(TAG, "role: receiver");

    uint8_t peer[6];
    if (csi_wifi_parse_mac(CONFIG_CSI_PEER_MAC, peer)) {
        csi_capture_set_peer(peer);
    } else {
        // Without a filter every device on the channel contributes frames, at a rate you do not
        // control and from geometry you do not know. Usable for a first smoke test, wrong for
        // anything after that.
        ESP_LOGW(TAG, "no peer MAC configured — capturing CSI from every station on the channel");
        csi_capture_set_peer(NULL);
    }

    ESP_ERROR_CHECK(csi_net_start(&s_ring, CONFIG_CSI_SERVER_HOST, CONFIG_CSI_SERVER_PORT));
    ESP_ERROR_CHECK(csi_wifi_start_promiscuous());
    ESP_ERROR_CHECK(csi_capture_start(&s_ring));
#endif

    xTaskCreatePinnedToCore(report_task, "csi_report", 3072, NULL, 1, NULL, 1);
}
