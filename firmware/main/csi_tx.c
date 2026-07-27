// Transmitter role: put frames in the air at a known, steady rate.
//
// This is the half of the ESP32 pair that makes the sampling rate a property of the system
// rather than of whatever your router happens to be doing. It sends a small UDP datagram every
// 1/rate seconds; the receiver hears the over-the-air transmission and reports CSI for it.
//
// The payload is deliberately tiny and its content irrelevant — what matters is that a frame
// was transmitted, from this MAC, at this instant.

#include "csi_tx.h"

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

static const char *TAG = "csi.tx";

typedef struct {
    uint32_t seq;
    uint64_t t_us;
} __attribute__((packed)) tx_payload_t;

static void tx_task(void *arg) {
    (void)arg;

    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        vTaskDelete(NULL);
        return;
    }

    int broadcast = 1;
    setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &broadcast, sizeof(broadcast));

    struct sockaddr_in dest = {0};
    dest.sin_family = AF_INET;
    dest.sin_port = htons(CONFIG_CSI_TX_PORT);
    if (inet_pton(AF_INET, CONFIG_CSI_TX_TARGET, &dest.sin_addr) != 1) {
        ESP_LOGE(TAG, "bad target %s", CONFIG_CSI_TX_TARGET);
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    const TickType_t period = pdMS_TO_TICKS(1000 / CONFIG_CSI_TX_RATE_HZ);
    ESP_LOGI(TAG, "transmitting to %s:%d at %d Hz (period %u ticks) on core %d",
             CONFIG_CSI_TX_TARGET, CONFIG_CSI_TX_PORT, CONFIG_CSI_TX_RATE_HZ,
             (unsigned)period, xPortGetCoreID());

    if (period == 0) {
        // A tick is 10 ms by default, which caps the rate at 100 Hz. Above that the delay
        // rounds to zero and the task spins. sdkconfig.defaults raises the tick rate to 1000 Hz
        // so 80-100 Hz lands on a clean multiple; if that was undone, say so rather than
        // quietly transmitting flat out.
        ESP_LOGE(TAG, "tick rate too coarse for %d Hz; raise CONFIG_FREERTOS_HZ",
                 CONFIG_CSI_TX_RATE_HZ);
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    tx_payload_t payload = {0};
    TickType_t last_wake = xTaskGetTickCount();

    while (true) {
        payload.seq++;
        payload.t_us = (uint64_t)esp_timer_get_time();
        sendto(sock, &payload, sizeof(payload), 0, (struct sockaddr *)&dest, sizeof(dest));

        // vTaskDelayUntil, not vTaskDelay: the deadline is absolute, so a slow send does not
        // push every subsequent transmission later. Rate stability is the entire point of this
        // node existing.
        vTaskDelayUntil(&last_wake, period);
    }
}

esp_err_t csi_tx_start(void) {
    // Core 1, same reasoning as the CSI sender: keep application transmits off the core the
    // WiFi driver runs on.
    BaseType_t ok = xTaskCreatePinnedToCore(tx_task, "csi_tx", 4096, NULL, 5, NULL, 1);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
