#include "csi_wifi.h"

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs_flash.h"

static const char *TAG = "csi.wifi";

#define CONNECTED_BIT BIT0
#define FAILED_BIT BIT1

static EventGroupHandle_t s_events;
static int s_retries;

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retries < CONFIG_CSI_WIFI_MAX_RETRY) {
            s_retries++;
            ESP_LOGW(TAG, "disconnected, retry %d", s_retries);
            esp_wifi_connect();
        } else {
            xEventGroupSetBits(s_events, FAILED_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&event->ip_info.ip));
        s_retries = 0;
        xEventGroupSetBits(s_events, CONNECTED_BIT);
    }
}

esp_err_t csi_wifi_start_sta(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    s_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &on_wifi_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &on_wifi_event, NULL, NULL));

    wifi_config_t config = {0};
    strncpy((char *)config.sta.ssid, CONFIG_CSI_WIFI_SSID, sizeof(config.sta.ssid) - 1);
    strncpy((char *)config.sta.password, CONFIG_CSI_WIFI_PASSWORD,
            sizeof(config.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));

    // Power save must be off. With it on the radio sleeps between beacons, which turns a steady
    // 80 Hz stream into bursts — and burst-sampled data is useless for frequency analysis no
    // matter how good the timestamps are.
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(s_events, CONNECTED_BIT | FAILED_BIT, pdFALSE, pdFALSE,
                                           portMAX_DELAY);
    if (!(bits & CONNECTED_BIT)) {
        ESP_LOGE(TAG, "could not join %s", CONFIG_CSI_WIFI_SSID);
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t csi_wifi_start_promiscuous(void) {
    // Promiscuous mode with no packet-type filter: CSI is reported for data frames from any
    // source on this channel, and csi_capture's MAC filter narrows it to the node we care
    // about. Filtering there rather than here keeps the radio configuration simple and puts
    // the decision next to the counter that reports how much it rejected.
    wifi_promiscuous_filter_t filter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA,
    };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_LOGI(TAG, "promiscuous mode on channel %u", csi_wifi_channel());
    return ESP_OK;
}

bool csi_wifi_parse_mac(const char *text, uint8_t out[6]) {
    if (text == NULL) {
        return false;
    }
    unsigned values[6];
    if (sscanf(text, "%x:%x:%x:%x:%x:%x", &values[0], &values[1], &values[2], &values[3],
               &values[4], &values[5]) != 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        if (values[i] > 0xFF) {
            return false;
        }
        out[i] = (uint8_t)values[i];
    }
    return true;
}

uint8_t csi_wifi_channel(void) {
    uint8_t primary = 0;
    wifi_second_chan_t secondary = WIFI_SECOND_CHAN_NONE;
    esp_wifi_get_channel(&primary, &secondary);
    return primary;
}
