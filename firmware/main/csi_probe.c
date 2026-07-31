// Station role: make the access point talk to us at a known, steady rate.
//
// This is the file that replaces the second board. A station that only listens gets CSI
// whenever the AP happens to address it, which on an idle home network is a handful of frames a
// second — and a spectrum computed from that is a spectrum of the household's traffic pattern,
// not of the room. So the node provokes its own downlink stream: send a small packet that
// demands a reply, and every reply that comes back is one CSI sample of the AP-to-node link.
//
// Which target does not matter much, because the last hop of any reply is the access point this
// node is associated with. The gateway is the default because it is one hop away, needs no
// configuration, and adds the least jitter of anything reachable.
//
// What this cannot do that the transmitter board could: guarantee the reply. The rate here is a
// request, not a fact, so the yield (replies over probes) is reported and is the number to watch.

#include "csi_probe.h"

#if !CONFIG_CSI_ROLE_STATION

// Compiled into every image, but CSI_PROBE_* only exist when the station role is selected.
esp_err_t csi_probe_start(void) { return ESP_ERR_NOT_SUPPORTED; }
void csi_probe_get_stats(csi_probe_stats_t *out) { *out = (csi_probe_stats_t){0}; }

#else

#include <errno.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/inet_chksum.h"
#include "lwip/sockets.h"

#if CONFIG_CSI_PROBE_ICMP
#include "lwip/icmp.h"
#endif

#include "csi_wifi.h"

static const char *TAG = "csi.probe";

// Enough that the frame carrying the reply is a normal-sized data frame rather than a runt, and
// small enough to be invisible on any link. The content is irrelevant — what matters is that the
// access point transmitted *something* to us at this instant.
#define PROBE_PAYLOAD_BYTES 24

static csi_probe_stats_t s_stats;

#if CONFIG_CSI_PROBE_ICMP
typedef struct {
    struct icmp_echo_hdr header;
    uint8_t payload[PROBE_PAYLOAD_BYTES];
} probe_packet_t;

static void build_echo_request(probe_packet_t *packet, uint16_t id, uint16_t seq) {
    ICMPH_TYPE_SET(&packet->header, ICMP_ECHO);
    ICMPH_CODE_SET(&packet->header, 0);
    packet->header.id = htons(id);
    packet->header.seqno = htons(seq);
    // Checksum is computed over the header *with the checksum field zeroed*, then written back.
    // Getting this backwards produces packets the router silently discards, which reads from
    // here as a router that rate-limits ICMP.
    packet->header.chksum = 0;
    packet->header.chksum = inet_chksum(packet, sizeof(*packet));
}
#endif

// Reads and discards whatever has arrived. Two reasons, and the second is the interesting one:
// an unread socket buffer fills and stops accepting, and the count of replies seen here is an
// independent check on the CSI path. Replies arriving while CSI frames do not means the capture
// configuration is wrong, not that the network is dropping probes — two very different problems
// that otherwise look identical from the statistics log.
static void drain(int sock) {
    uint8_t scratch[128];
    while (recv(sock, scratch, sizeof(scratch), MSG_DONTWAIT) > 0) {
        s_stats.replies++;
    }
}

static void probe_task(void *arg) {
    (void)arg;

    struct sockaddr_in dest = {0};
    dest.sin_family = AF_INET;

#if CONFIG_CSI_PROBE_ICMP
    const char *method = "ICMP echo";
    int sock = socket(AF_INET, SOCK_RAW, IPPROTO_ICMP);
    if (sock < 0) {
        ESP_LOGE(TAG, "raw socket failed: errno %d (is LWIP_RAW enabled?)", errno);
        vTaskDelete(NULL);
        return;
    }
    dest.sin_port = 0;
    if (strlen(CONFIG_CSI_PROBE_TARGET) > 0) {
        if (inet_pton(AF_INET, CONFIG_CSI_PROBE_TARGET, &dest.sin_addr) != 1) {
            ESP_LOGE(TAG, "bad probe target %s", CONFIG_CSI_PROBE_TARGET);
            close(sock);
            vTaskDelete(NULL);
            return;
        }
    } else {
        dest.sin_addr.s_addr = csi_wifi_gateway();
        if (dest.sin_addr.s_addr == 0) {
            ESP_LOGE(TAG, "no gateway from DHCP; set CSI_PROBE_TARGET explicitly");
            close(sock);
            vTaskDelete(NULL);
            return;
        }
    }
#else
    const char *method = "UDP echo";
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        vTaskDelete(NULL);
        return;
    }
    dest.sin_port = htons(CONFIG_CSI_ECHO_PORT);
    if (inet_pton(AF_INET, CONFIG_CSI_SERVER_HOST, &dest.sin_addr) != 1) {
        ESP_LOGE(TAG, "bad server address %s", CONFIG_CSI_SERVER_HOST);
        close(sock);
        vTaskDelete(NULL);
        return;
    }
#endif

    const TickType_t period = pdMS_TO_TICKS(1000 / CONFIG_CSI_PROBE_RATE_HZ);
    if (period == 0) {
        // A tick is 10 ms by default, which caps the rate at 100 Hz. Above that the delay rounds
        // to zero and the task spins. sdkconfig.defaults raises the tick rate to 1000 Hz so
        // 80-100 Hz lands on a clean multiple; if that was undone, say so rather than quietly
        // probing flat out.
        ESP_LOGE(TAG, "tick rate too coarse for %d Hz; raise CONFIG_FREERTOS_HZ",
                 CONFIG_CSI_PROBE_RATE_HZ);
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    const uint32_t addr = dest.sin_addr.s_addr;  // network order: low byte is the first octet
    ESP_LOGI(TAG, "probing %u.%u.%u.%u at %d Hz (%s, period %u ticks) on core %d",
             (unsigned)(addr & 0xFF), (unsigned)((addr >> 8) & 0xFF),
             (unsigned)((addr >> 16) & 0xFF), (unsigned)((addr >> 24) & 0xFF),
             CONFIG_CSI_PROBE_RATE_HZ, method, (unsigned)period, xPortGetCoreID());

#if CONFIG_CSI_PROBE_ICMP
    const uint16_t id = (uint16_t)(CONFIG_CSI_NODE_ID | 0xC500);
    probe_packet_t packet;
    memset(&packet, 0, sizeof(packet));
#else
    struct {
        uint32_t seq;
        uint64_t t_us;
        uint8_t pad[PROBE_PAYLOAD_BYTES];
    } __attribute__((packed)) packet = {0};
#endif

    uint16_t seq = 0;
    TickType_t last_wake = xTaskGetTickCount();

    while (true) {
#if CONFIG_CSI_PROBE_ICMP
        build_echo_request(&packet, id, seq);
#else
        packet.seq = seq;
        packet.t_us = (uint64_t)esp_timer_get_time();
#endif
        seq++;

        if (sendto(sock, &packet, sizeof(packet), 0, (struct sockaddr *)&dest, sizeof(dest)) < 0) {
            s_stats.send_errors++;
        } else {
            s_stats.probes++;
        }

        drain(sock);

        // vTaskDelayUntil, not vTaskDelay: the deadline is absolute, so a slow send does not push
        // every subsequent probe later. Rate stability is the entire point of this task existing.
        vTaskDelayUntil(&last_wake, period);
    }
}

esp_err_t csi_probe_start(void) {
    memset(&s_stats, 0, sizeof(s_stats));
    // Core 1, same reasoning as the CSI sender: keep application transmits off the core the
    // WiFi driver runs on.
    BaseType_t ok = xTaskCreatePinnedToCore(probe_task, "csi_probe", 4096, NULL,
                                            CONFIG_CSI_PROBE_PRIORITY, NULL, 1);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

void csi_probe_get_stats(csi_probe_stats_t *out) { *out = s_stats; }

#endif  // CONFIG_CSI_ROLE_STATION
