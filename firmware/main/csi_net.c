// The packer/sender task. Runs on Core 1, drains the ring, sends one datagram per frame.
//
// Pinning to Core 1 is only half the job. The lwIP TCP/IP task defaults to Core 0, so an
// unpinned stack would put the actual `sendto` work right back on the core the WiFi driver
// runs on, and the contention that pinning was supposed to remove would still be there — just
// somewhere less obvious. `CONFIG_LWIP_TCPIP_TASK_AFFINITY_CPU1` in sdkconfig.defaults is the
// other half. Both are needed; neither alone is sufficient.
//
// (Dual-core parts only. The C3 and C6 have no Core 1, and there both settings are no-ops.)

#include "csi_net.h"

#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

#include "csi_capture.h"
#include "csi_ring.h"

static const char *TAG = "csi.net";

static csi_ring_t *s_ring;
static int s_sock = -1;
static struct sockaddr_in s_dest;
static TaskHandle_t s_task;
static csi_net_stats_t s_stats;

// How long the sender blocks waiting to be woken. It is woken per frame, so this only matters
// when the stream stops: the task still ticks over occasionally to publish statistics.
#define WAIT_TICKS pdMS_TO_TICKS(200)

static int open_socket(void) {
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        return -1;
    }

    // A send buffer big enough that a brief scheduling hiccup queues rather than fails.
    int sndbuf = 32 * 1024;
    setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    // Non-blocking. If the stack is momentarily out of buffers we would rather drop this frame
    // — the server treats it as a sequence gap — than stall the task and build a backlog that
    // eventually overflows the ring behind us.
    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);
    return sock;
}

static void sender_task(void *arg) {
    (void)arg;
    ESP_LOGI(TAG, "sender running on core %d", xPortGetCoreID());

    while (true) {
        ulTaskNotifyTake(pdTRUE, WAIT_TICKS);

        if (s_sock < 0) {
            s_sock = open_socket();
            if (s_sock < 0) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }
        }

        const csi_slot_t *slot;
        while ((slot = csi_ring_peek(s_ring)) != NULL) {
            int sent = sendto(s_sock, slot->data, slot->len, 0, (struct sockaddr *)&s_dest,
                              sizeof(s_dest));
            if (sent < 0) {
                s_stats.send_errors++;
                if (errno != EWOULDBLOCK && errno != EAGAIN) {
                    // A real error (no route, interface down). Reopen on the next wake rather
                    // than spinning on a dead socket.
                    ESP_LOGW(TAG, "sendto failed: errno %d, reopening", errno);
                    close(s_sock);
                    s_sock = -1;
                    break;
                }
                // Out of buffers. Drop this frame and move on; blocking here is what we are
                // specifically trying not to do.
            } else {
                s_stats.sent++;
                s_stats.bytes += (uint32_t)sent;
            }
            csi_ring_pop_done(s_ring);
        }
    }
}

esp_err_t csi_net_start(csi_ring_t *ring, const char *host, uint16_t port) {
    s_ring = ring;
    memset(&s_stats, 0, sizeof(s_stats));

    memset(&s_dest, 0, sizeof(s_dest));
    s_dest.sin_family = AF_INET;
    s_dest.sin_port = htons(port);
    if (inet_pton(AF_INET, host, &s_dest.sin_addr) != 1) {
        ESP_LOGE(TAG, "bad server address %s", host);
        return ESP_ERR_INVALID_ARG;
    }

    // Core 1. See the note at the top of this file about lwIP's affinity.
    BaseType_t ok = xTaskCreatePinnedToCore(sender_task, "csi_send", 4096, NULL,
                                            CONFIG_CSI_SENDER_PRIORITY, &s_task, 1);
    if (ok != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    csi_capture_set_consumer(s_task);
    ESP_LOGI(TAG, "sending CSI to %s:%u", host, port);
    return ESP_OK;
}

void csi_net_get_stats(csi_net_stats_t *out) {
    *out = s_stats;
    out->queued = csi_ring_count(s_ring);
}
