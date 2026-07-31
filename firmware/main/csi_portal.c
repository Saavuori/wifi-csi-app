#include "csi_portal.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "driver/gpio.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "csi_settings.h"
#include "csi_wifi.h"

static const char *TAG = "csi.portal";

// Same cap as the boot scan in csi_wifi.c, and the same reasoning: enough for a large house, and
// the count is reported so a truncated list is not mistaken for a complete one.
#define SCAN_MAX_APS 24

// The page, linked in by CMakeLists' EMBED_TXTFILES. Kept as a file rather than a C string so it
// stays editable and diffable as HTML.
extern const uint8_t portal_html_start[] asm("_binary_portal_html_start");
extern const uint8_t portal_html_end[] asm("_binary_portal_html_end");

// A save writes NVS and then reboots, but it has to answer the browser first — a connection
// dropped mid-response looks like a failure from the other end, and the operator retries a save
// that already happened. The reboot is handed to this task instead, which lets the response
// finish first.
static void reboot_task(void *arg) {
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(1200));
    ESP_LOGI(TAG, "restarting into the configured network");
    esp_restart();
}

static esp_err_t send_json(httpd_req_t *req, const char *status, cJSON *json) {
    char *text = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    if (text == NULL) {
        return httpd_resp_send_500(req);
    }
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    esp_err_t err = httpd_resp_sendstr(req, text);
    cJSON_free(text);
    return err;
}

static esp_err_t send_error(httpd_req_t *req, const char *status, const char *message) {
    cJSON *json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "error", message);
    return send_json(req, status, json);
}

static esp_err_t get_index(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, (const char *)portal_html_start,
                           portal_html_end - portal_html_start - 1);
}

static esp_err_t get_current(httpd_req_t *req) {
    const csi_settings_t *settings = csi_settings();
    cJSON *json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "ssid", settings->ssid);
    // The password is deliberately not here. It would be readable by anyone who can reach the
    // setup network, which during provisioning is anyone within range.
    cJSON_AddBoolToObject(json, "has_password", settings->password[0] != '\0');
    cJSON_AddStringToObject(json, "lock_bssid", settings->lock_bssid);
    cJSON_AddStringToObject(json, "server_host", settings->server_host);
    cJSON_AddNumberToObject(json, "server_port", settings->server_port);
    cJSON_AddNumberToObject(json, "node_id", settings->node_id);
    cJSON_AddNumberToObject(json, "probe_rate_hz", settings->probe_rate_hz);
    cJSON_AddBoolToObject(json, "provisioned", csi_settings_provisioned());
    return send_json(req, "200 OK", json);
}

// Lists every access point in range, not only those on one SSID — the portal is where the SSID
// is chosen, so filtering by it would be circular. Sorted by signal, because the first question
// asked of this list is always which access points are actually reachable from here.
static esp_err_t get_scan(httpd_req_t *req) {
    wifi_scan_config_t scan = {.show_hidden = false};
    esp_err_t err = esp_wifi_scan_start(&scan, true);
    if (err != ESP_OK) {
        return send_error(req, "503 Service Unavailable", esp_err_to_name(err));
    }

    uint16_t found = 0;
    esp_wifi_scan_get_ap_num(&found);

    static wifi_ap_record_t records[SCAN_MAX_APS];
    uint16_t count = SCAN_MAX_APS;
    if (esp_wifi_scan_get_ap_records(&count, records) != ESP_OK) {
        return send_error(req, "503 Service Unavailable", "could not read scan results");
    }

    for (uint16_t i = 0; i + 1 < count; i++) {
        for (uint16_t j = i + 1; j < count; j++) {
            if (records[j].rssi > records[i].rssi) {
                wifi_ap_record_t swap = records[i];
                records[i] = records[j];
                records[j] = swap;
            }
        }
    }

    cJSON *json = cJSON_CreateObject();
    cJSON_AddNumberToObject(json, "found", found);
    cJSON *aps = cJSON_AddArrayToObject(json, "aps");
    for (uint16_t i = 0; i < count; i++) {
        char bssid[CSI_MAC_TEXT_MAX];
        snprintf(bssid, sizeof(bssid), "%02x:%02x:%02x:%02x:%02x:%02x", records[i].bssid[0],
                 records[i].bssid[1], records[i].bssid[2], records[i].bssid[3], records[i].bssid[4],
                 records[i].bssid[5]);
        cJSON *ap = cJSON_CreateObject();
        cJSON_AddStringToObject(ap, "bssid", bssid);
        cJSON_AddStringToObject(ap, "ssid", (const char *)records[i].ssid);
        cJSON_AddNumberToObject(ap, "channel", records[i].primary);
        cJSON_AddNumberToObject(ap, "rssi", records[i].rssi);
        cJSON_AddItemToArray(aps, ap);
    }
    return send_json(req, "200 OK", json);
}

// Copies a JSON string field into a fixed buffer, leaving the buffer untouched when the field is
// absent. Absent and empty are different here: an absent password means "keep the stored one",
// which is what lets the page be used to change the probe rate without knowing the WiFi password.
static void take_string(const cJSON *root, const char *name, char *out, size_t size) {
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    if (cJSON_IsString(item) && item->valuestring != NULL) {
        snprintf(out, size, "%s", item->valuestring);
    }
}

static void take_number(const cJSON *root, const char *name, uint16_t *out) {
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    if (cJSON_IsNumber(item)) {
        *out = (uint16_t)item->valuedouble;
    }
}

static esp_err_t post_save(httpd_req_t *req) {
    if (req->content_len <= 0 || req->content_len > 1024) {
        return send_error(req, "400 Bad Request", "unexpected body size");
    }

    char body[1025];
    int received = 0;
    while (received < req->content_len) {
        int n = httpd_req_recv(req, body + received, req->content_len - received);
        if (n <= 0) {
            return send_error(req, "400 Bad Request", "truncated body");
        }
        received += n;
    }
    body[received] = '\0';

    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        return send_error(req, "400 Bad Request", "body is not JSON");
    }

    // Start from what is stored, so a field the page did not send keeps its value rather than
    // reverting to the compiled-in default.
    csi_settings_t next = *csi_settings();
    take_string(root, "ssid", next.ssid, sizeof(next.ssid));
    take_string(root, "password", next.password, sizeof(next.password));
    take_string(root, "lock_bssid", next.lock_bssid, sizeof(next.lock_bssid));
    take_string(root, "server_host", next.server_host, sizeof(next.server_host));
    take_number(root, "server_port", &next.server_port);
    take_number(root, "probe_rate_hz", &next.probe_rate_hz);

    const cJSON *node = cJSON_GetObjectItemCaseSensitive(root, "node_id");
    if (cJSON_IsNumber(node)) {
        next.node_id = (uint8_t)node->valuedouble;
    }
    cJSON_Delete(root);

    const char *problem = csi_settings_validate(&next);
    if (problem != NULL) {
        ESP_LOGW(TAG, "rejected settings: %s", problem);
        return send_error(req, "400 Bad Request", problem);
    }

    esp_err_t err = csi_settings_save(&next);
    if (err != ESP_OK) {
        return send_error(req, "500 Internal Server Error", esp_err_to_name(err));
    }

    cJSON *ok = cJSON_CreateObject();
    cJSON_AddBoolToObject(ok, "saved", true);
    esp_err_t sent = send_json(req, "200 OK", ok);
    xTaskCreate(reboot_task, "csi_reboot", 2048, NULL, 1, NULL);
    return sent;
}

bool csi_portal_button_held(void) {
    const gpio_num_t pin = CONFIG_CSI_PORTAL_BUTTON_GPIO;
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    if (gpio_config(&config) != ESP_OK) {
        return false;
    }

    // The button is watched for a window *after* boot, not sampled at boot.
    //
    // This is not a nicety. On the usual DevKit boards this is GPIO0, the same pin the ROM
    // bootloader strobes to decide whether to enter serial download mode — so a button held
    // down through reset never reaches this code at all, it lands in the bootloader. The only
    // moment the application can observe is after the ROM has already made that decision, which
    // means the press has to happen *after* the board is up.
    //
    // Hence a window wide enough to press into by hand, rather than an instant to hit.
    const int window_ms = CONFIG_CSI_PORTAL_BUTTON_WINDOW_MS;
    const int step_ms = 25;
    // Continuous, not cumulative: a bouncing contact or a floating pin should not accumulate its
    // way past the threshold over several seconds.
    const int needed = 500 / step_ms;

    ESP_LOGI(TAG, "hold the button on GPIO%d within %d ms to open the setup portal", (int)pin,
             window_ms);

    int low = 0;
    for (int elapsed = 0; elapsed < window_ms; elapsed += step_ms) {
        if (gpio_get_level(pin) == 0) {
            if (++low >= needed) {
                return true;
            }
        } else {
            low = 0;
        }
        vTaskDelay(pdMS_TO_TICKS(step_ms));
    }
    return false;
}

// Shared by both entry points, so the page and the API cannot drift between the setup network
// and the live one.
// `alongside_capture` is the difference between the two callers: on the setup network nothing is
// being measured, so the defaults are right, while on the live network the server has to keep
// out of the capture path's way.
static esp_err_t start_http(httpd_handle_t *server, bool alongside_capture) {
    httpd_config_t http = HTTPD_DEFAULT_CONFIG();
    http.lru_purge_enable = true;
    if (alongside_capture) {
        http.core_id = 1;
        // Below the sender (CSI_SENDER_PRIORITY, 6) and the prober (CSI_PROBE_PRIORITY, 5).
        http.task_priority = 2;
    }

    esp_err_t err = httpd_start(server, &http);
    if (err != ESP_OK) {
        return err;
    }

    const httpd_uri_t routes[] = {
        {.uri = "/", .method = HTTP_GET, .handler = get_index},
        {.uri = "/api/current", .method = HTTP_GET, .handler = get_current},
        {.uri = "/api/scan", .method = HTTP_GET, .handler = get_scan},
        {.uri = "/api/save", .method = HTTP_POST, .handler = post_save},
    };
    for (size_t i = 0; i < sizeof(routes) / sizeof(routes[0]); i++) {
        err = httpd_register_uri_handler(*server, &routes[i]);
        if (err != ESP_OK) {
            return err;
        }
    }
    return ESP_OK;
}

#if CONFIG_CSI_PORTAL_IN_STATION
esp_err_t csi_portal_start_station(void) {
    // Core 1 and a priority below the sender's (6) and the prober's (5).
    //
    // The reasoning behind keeping this off Core 0 is the same one that pins the packer: Core 0
    // is the WiFi driver's, and the CSI callback runs there. Core 1 already carries the sender
    // and the prober, and this sits under both — a request served late costs a slow page, where
    // a CSI frame served late costs a queue that never drains.
    //
    // Idle, it is one task blocked in select() on a listening socket and about 12 KB of heap. It
    // does real work only while someone has the page open, which is a deliberate act by someone
    // who is about to reboot the node anyway.
    httpd_handle_t server = NULL;
    esp_err_t err = start_http(&server, true);
    if (err != ESP_OK) {
        // Not fatal. The node's job is to measure; losing the convenience of a settings page is
        // not a reason to refuse to do it.
        ESP_LOGW(TAG, "setup page not available: %s", esp_err_to_name(err));
        return err;
    }

    const uint32_t addr = csi_wifi_ip();  // network order: low byte is the first octet
    ESP_LOGI(TAG, "setup page at http://%u.%u.%u.%u/", (unsigned)(addr & 0xFF),
             (unsigned)((addr >> 8) & 0xFF), (unsigned)((addr >> 16) & 0xFF),
             (unsigned)((addr >> 24) & 0xFF));
    return ESP_OK;
}
#else
esp_err_t csi_portal_start_station(void) { return ESP_ERR_NOT_SUPPORTED; }
#endif

esp_err_t csi_portal_run(const char *reason) {
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);

    char ssid[CSI_SSID_MAX];
    // The last three MAC octets, so two nodes being set up in the same room are distinguishable
    // without a serial console.
    snprintf(ssid, sizeof(ssid), "%s-%02x%02x%02x", CONFIG_CSI_PORTAL_AP_PREFIX, mac[3], mac[4],
             mac[5]);

    ESP_ERROR_CHECK(esp_netif_init());
    // May already exist when the portal is entered after a failed join rather than at first
    // boot; that is not an error worth aborting on.
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    esp_netif_t *ap_netif = esp_netif_create_default_wifi_ap();
    // The station interface exists only so the portal can scan. APSTA rather than AP: a scan
    // requires the station side, and scanning is the whole reason to prefer this page over
    // typing a BSSID from memory.
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));

    wifi_config_t ap = {0};
    // ap.ssid is the raw 32-byte 802.11 field, not a C string, so the length is carried
    // separately and the copy is bounded explicitly rather than by snprintf's terminator.
    size_t ssid_len = strlen(ssid);
    if (ssid_len > sizeof(ap.ap.ssid)) {
        ssid_len = sizeof(ap.ap.ssid);
        ssid[ssid_len] = '\0';  // so the log below shows what was actually advertised
    }
    memcpy(ap.ap.ssid, ssid, ssid_len);
    ap.ap.ssid_len = ssid_len;
    ap.ap.channel = 1;
    ap.ap.max_connection = 2;
    if (strlen(CONFIG_CSI_PORTAL_AP_PASSWORD) >= 8) {
        snprintf((char *)ap.ap.password, sizeof(ap.ap.password), "%s",
                 CONFIG_CSI_PORTAL_AP_PASSWORD);
        ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        // Open, and briefly. The alternative is a shared default password printed in a README,
        // which is not meaningfully better, and an open network makes it obvious that this is a
        // setup mode rather than something to leave running.
        ap.ap.authmode = WIFI_AUTH_OPEN;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());

    esp_netif_ip_info_t ip;
    esp_netif_get_ip_info(ap_netif, &ip);

    ESP_LOGW(TAG, "-----------------------------------------------------------");
    ESP_LOGW(TAG, "provisioning portal: %s", reason);
    ESP_LOGW(TAG, "  join WiFi network \"%s\"%s", ssid,
             ap.ap.authmode == WIFI_AUTH_OPEN ? " (open)" : "");
    ESP_LOGW(TAG, "  then open http://" IPSTR, IP2STR(&ip.ip));
    ESP_LOGW(TAG, "-----------------------------------------------------------");

    // Nothing is being measured here, so there is no core to keep clear and no priority to sit
    // under: the defaults are right.
    httpd_handle_t server = NULL;
    ESP_ERROR_CHECK(start_http(&server, false));

    // Never returns. Saving reboots; there is no path from here into capture, because capture
    // reads its configuration once at startup and this is how that startup gets its values.
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}
