// ESP32-S3 CSI node.
//
// Three roles, chosen in menuconfig. The first is the one to start with:
//
//   STATION      one board, joined to the WiFi you already have. It probes the access point at
//                a fixed rate and reports CSI for the replies, so the link it measures is
//                AP-to-node. No second radio to place, power, or configure.
//   RECEIVER     listens promiscuously and reports CSI for frames from a second ESP32.
//   TRANSMITTER  that second ESP32: emits a small datagram at a fixed rate.
//
// RECEIVER + TRANSMITTER is the plan's topology B. It buys a link nobody else shares — you own
// both endpoints, so the rate is a fact rather than a request — at the cost of a second board.
// The station role gives that up: the reply rate depends on the access point's load, and the
// number to watch is the yield in the statistics line below. Start with one board; add the
// second only if the yield says you have to.
//
// On a mesh network, set CSI_LOCK_BSSID. Several access points share the SSID, and being moved
// between them changes the geometry of every measurement with nothing to show for it in the
// data. The lock turns a silent steering into a visible disconnect. csi_wifi.c has the detail.

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "csi_capture.h"
#include "csi_net.h"
#include "csi_probe.h"
#include "csi_ring.h"
#include "csi_tx.h"
#include "csi_wifi.h"

static const char *TAG = "csi";

// Statically allocated. About 26 KB at the default slot count — worth it to have the ring exist
// before the WiFi driver does, and to make it impossible for a CSI callback to arrive while
// the buffer it writes into is still being allocated.
static csi_ring_t s_ring;

#if CONFIG_CSI_ROLE_STATION
// Runs on every association. Re-arming the filter here rather than once at startup is what makes
// a reconnect survivable: with a BSSID lock the address is the same every time, but without one
// the network is free to hand us a different access point, and a filter still pointing at the
// old one would reject every frame forever while every counter looked healthy.
static void on_link_up(const uint8_t bssid[6], uint8_t epoch) {
    csi_capture_set_peer(bssid);
    csi_capture_set_link_epoch(epoch);
}
#endif

static void report_task(void *arg) {
    (void)arg;
    csi_capture_stats_t capture_prev = {0};
    csi_net_stats_t net_prev = {0};
    const int period_s = CONFIG_CSI_REPORT_PERIOD_S;
#if CONFIG_CSI_ROLE_STATION
    csi_probe_stats_t probe_prev = {0};
#endif

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

#if CONFIG_CSI_ROLE_STATION
        csi_probe_stats_t probe;
        csi_probe_get_stats(&probe);
        const uint32_t probes = probe.probes - probe_prev.probes;

        // Yield is the station role's headline number and has no equivalent in the pair
        // topology: it is the fraction of probes that came back as a CSI measurement. Well under
        // 1.0 means the access point is not answering every probe — a busy channel, or a router
        // rate-limiting ICMP — and it is the honest cost of not owning the link. The reply
        // counter next to it separates the two ways that can happen: replies arriving without
        // CSI frames is a capture problem, not a network one.
        uint8_t bssid[6] = {0};
        csi_wifi_bssid(bssid);
        ESP_LOGI(TAG,
                 "link %02x:%02x:%02x:%02x:%02x:%02x epoch %u | probes %u | replies %u | "
                 "yield %.0f%% | probe errors %u | disconnects %u",
                 bssid[0], bssid[1], bssid[2], bssid[3], bssid[4], bssid[5],
                 csi_wifi_link_epoch(), (unsigned)probes,
                 (unsigned)(probe.replies - probe_prev.replies),
                 probes ? 100.0f * (float)frames / (float)probes : 0.0f,
                 (unsigned)probe.send_errors, (unsigned)csi_wifi_disconnects());
        probe_prev = probe;
#endif

        capture_prev = capture;
        net_prev = net;
    }
}

void app_main(void) {
    csi_ring_init(&s_ring);

    ESP_LOGI(TAG, "node %d starting", CONFIG_CSI_NODE_ID);

#if CONFIG_CSI_ROLE_STATION
    // Registered before the join, or the first association goes past unseen and capture starts
    // with no filter and epoch 0.
    csi_wifi_set_link_cb(on_link_up);
#endif

    ESP_ERROR_CHECK(csi_wifi_start_sta());

#if CONFIG_CSI_ROLE_TRANSMITTER
    ESP_LOGI(TAG, "role: transmitter");
    ESP_ERROR_CHECK(csi_tx_start());

#elif CONFIG_CSI_ROLE_STATION
    ESP_LOGI(TAG, "role: station");

    // No promiscuous mode here, unlike the receiver role. A station already gets a CSI callback
    // for the frames the access point addressed to it, and those frames *are* the link we want
    // to measure. Sniffing the whole channel would only hand the callback more to reject.
    ESP_ERROR_CHECK(csi_net_start(&s_ring, CONFIG_CSI_SERVER_HOST, CONFIG_CSI_SERVER_PORT));
    ESP_ERROR_CHECK(csi_capture_start(&s_ring));
    // Last, and after the join: the default probe target is the DHCP gateway, which does not
    // exist until there is a lease.
    ESP_ERROR_CHECK(csi_probe_start());

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
