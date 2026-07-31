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

#include "csi_settings.h"

static const char *TAG = "csi.wifi";

#define CONNECTED_BIT BIT0
#define FAILED_BIT BIT1
#define STARTED_BIT BIT2

// Enough to list a large house. A mesh with more access points than this is not a case worth
// carrying code for; the log says how many were seen so a truncated list is not mistaken for a
// complete one.
#define SCAN_MAX_APS 16

static EventGroupHandle_t s_events;
static int s_retries;
static uint8_t s_bssid[6];
static bool s_have_bssid;
static uint8_t s_link_epoch;
static uint32_t s_disconnects;
static uint32_t s_gateway;
static uint32_t s_ip;
static csi_wifi_link_cb_t s_link_cb;

void csi_wifi_set_link_cb(csi_wifi_link_cb_t cb) { s_link_cb = cb; }

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        // Deliberately does *not* connect here. The boot scan has to run between start and
        // connect, so csi_wifi_start_sta() drives the association itself and this event only
        // reports that the driver is ready for it.
        xEventGroupSetBits(s_events, STARTED_BIT);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        wifi_event_sta_connected_t *event = (wifi_event_sta_connected_t *)data;
        memcpy(s_bssid, event->bssid, 6);
        s_have_bssid = true;
        // The epoch advances on association rather than on getting an IP: the radio link is
        // what the measurement depends on, and a DHCP renewal that never dropped the link is
        // not a discontinuity.
        s_link_epoch++;
        ESP_LOGI(TAG, "associated with %02x:%02x:%02x:%02x:%02x:%02x on channel %u (link epoch %u)",
                 event->bssid[0], event->bssid[1], event->bssid[2], event->bssid[3],
                 event->bssid[4], event->bssid[5], event->channel, s_link_epoch);
        if (s_link_cb != NULL) {
            s_link_cb(s_bssid, s_link_epoch);
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_disconnects++;
        if (s_retries < CONFIG_CSI_WIFI_MAX_RETRY) {
            s_retries++;
            ESP_LOGW(TAG, "disconnected (%u since boot), retry %d", (unsigned)s_disconnects,
                     s_retries);
            esp_wifi_connect();
        } else {
            xEventGroupSetBits(s_events, FAILED_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        s_gateway = event->ip_info.gw.addr;
        s_ip = event->ip_info.ip.addr;
        ESP_LOGI(TAG, "got ip " IPSTR ", gateway " IPSTR, IP2STR(&event->ip_info.ip),
                 IP2STR(&event->ip_info.gw));
        s_retries = 0;
        xEventGroupSetBits(s_events, CONNECTED_BIT);
    }
}

// Lists every access point advertising our SSID. On the consumer mesh systems this firmware is
// aimed at, the phone app does not show BSSIDs at all, so this is the only practical way to
// find out which access points exist and how each one reads from where the node is sitting.
//
// Choosing between them is the real decision the log exists to support: the line between the
// chosen AP and this node is the line the system senses along, so the useful one is whichever
// AP puts a doorway, a bed, or a hallway on that line — not necessarily the strongest.
static void scan_and_log(void) {
    const csi_settings_t *settings = csi_settings();
    wifi_scan_config_t scan = {
        .ssid = (uint8_t *)settings->ssid,
        .show_hidden = false,
    };

    ESP_LOGI(TAG, "scanning for \"%s\" ...", settings->ssid);
    esp_err_t err = esp_wifi_scan_start(&scan, true);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "scan failed (%s); continuing to associate", esp_err_to_name(err));
        return;
    }

    uint16_t found = 0;
    esp_wifi_scan_get_ap_num(&found);

    // Static, not automatic: at 16 entries this is over a kilobyte, and app_main's stack also has
    // to carry WiFi initialisation and the association. It is written once, from one task, before
    // anything else is running.
    static wifi_ap_record_t records[SCAN_MAX_APS];
    uint16_t count = SCAN_MAX_APS;
    if (esp_wifi_scan_get_ap_records(&count, records) != ESP_OK) {
        return;
    }

    if (found > count) {
        ESP_LOGI(TAG, "%u access point(s) on this SSID, listing the first %u:", (unsigned)found,
                 (unsigned)count);
    } else {
        ESP_LOGI(TAG, "%u access point(s) on this SSID:", (unsigned)count);
    }
    for (uint16_t i = 0; i < count; i++) {
        ESP_LOGI(TAG, "  %02x:%02x:%02x:%02x:%02x:%02x  ch %2u  %4d dBm", records[i].bssid[0],
                 records[i].bssid[1], records[i].bssid[2], records[i].bssid[3],
                 records[i].bssid[4], records[i].bssid[5], records[i].primary, records[i].rssi);
    }
    ESP_LOGI(TAG, "lock the node to one of these on the setup page, or with CSI_LOCK_BSSID");
}

esp_err_t csi_wifi_start_sta(void) {
    const csi_settings_t *settings = csi_settings();

    // NVS is initialised by csi_settings_load(), which every path into here has already called —
    // it has to, because the SSID this function is about to use comes from it.
    s_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &on_wifi_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &on_wifi_event, NULL, NULL));

    wifi_config_t config = {0};
    strncpy((char *)config.sta.ssid, settings->ssid, sizeof(config.sta.ssid) - 1);
    strncpy((char *)config.sta.password, settings->password, sizeof(config.sta.password) - 1);

    // Protected management frames, capable but not required. Home mesh systems commonly run
    // WPA2/WPA3 transitional with PMF optional, and a station that advertises neither is
    // refused by some of them — a refusal that looks exactly like a wrong password from here.
    config.sta.pmf_cfg.capable = true;
    config.sta.pmf_cfg.required = false;
    if (config.sta.password[0] != '\0') {
        config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    }

    uint8_t locked[6];
    const bool lock = csi_wifi_parse_mac(settings->lock_bssid, locked);
    if (lock) {
        memcpy(config.sta.bssid, locked, 6);
        config.sta.bssid_set = true;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));

    // Power save must be off. With it on the radio sleeps between beacons, which turns a steady
    // 80 Hz stream into bursts — and burst-sampled data is useless for frequency analysis no
    // matter how good the timestamps are.
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_ERROR_CHECK(esp_wifi_start());
    xEventGroupWaitBits(s_events, STARTED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(5000));

#if CONFIG_CSI_SCAN_ON_BOOT
    scan_and_log();
#endif

    if (lock) {
        ESP_LOGI(TAG, "locked to %02x:%02x:%02x:%02x:%02x:%02x — this node will not roam",
                 locked[0], locked[1], locked[2], locked[3], locked[4], locked[5]);
    } else {
        // Worth a warning rather than a note. On a single-AP network this costs nothing; on a
        // mesh it means the network may move the node to another access point at any time,
        // which changes the geometry of every measurement without changing anything the
        // analysis can see. The server detects it after the fact from the link epoch and throws
        // the history away, which is correct but expensive — you lose the calibration.
        ESP_LOGW(TAG, "no BSSID lock is set: on a mesh network this node may be steered "
                      "between access points mid-session");
    }

    ESP_ERROR_CHECK(esp_wifi_connect());

    EventBits_t bits = xEventGroupWaitBits(s_events, CONNECTED_BIT | FAILED_BIT, pdFALSE, pdFALSE,
                                           portMAX_DELAY);
    if (!(bits & CONNECTED_BIT)) {
        ESP_LOGE(TAG, "could not join %s", settings->ssid);
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

bool csi_wifi_bssid(uint8_t out[6]) {
    if (!s_have_bssid) {
        return false;
    }
    memcpy(out, s_bssid, 6);
    return true;
}

uint8_t csi_wifi_link_epoch(void) { return s_link_epoch; }

uint32_t csi_wifi_disconnects(void) { return s_disconnects; }

uint32_t csi_wifi_gateway(void) { return s_gateway; }

uint32_t csi_wifi_ip(void) { return s_ip; }
