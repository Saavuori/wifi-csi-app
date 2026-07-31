#include "csi_settings.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "csi_wifi.h"

static const char *TAG = "csi.settings";

// One namespace, one blob. A blob rather than a key per field because the settings are only ever
// read and written as a set: a half-applied configuration — new SSID, old BSSID lock — is a node
// that fails to associate for a reason nothing in the log explains.
#define NVS_NAMESPACE "csi"
#define NVS_KEY "settings"

// Bumped when the struct layout changes. A blob written by an older firmware is discarded rather
// than reinterpreted, because reinterpreting it means an arbitrary BSSID lock and an arbitrary
// server address, and the node goes quiet somewhere in the house.
#define SETTINGS_VERSION 1

typedef struct {
    uint32_t version;
    csi_settings_t settings;
} stored_t;

static csi_settings_t s_settings = {
    .ssid = CONFIG_CSI_WIFI_SSID,
    .password = CONFIG_CSI_WIFI_PASSWORD,
    .lock_bssid = CONFIG_CSI_LOCK_BSSID,
    .server_host = CONFIG_CSI_SERVER_HOST,
    .server_port = CONFIG_CSI_SERVER_PORT,
#if CONFIG_CSI_ROLE_STATION
    .probe_rate_hz = CONFIG_CSI_PROBE_RATE_HZ,
#else
    .probe_rate_hz = 100,
#endif
    .node_id = CONFIG_CSI_NODE_ID,
};

static bool s_provisioned;

esp_err_t csi_settings_load(void) {
    // Every caller of this needs NVS anyway, and initialising it here means the portal does not
    // have to duplicate the erase-and-retry dance that a version change or a full partition
    // requires.
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        return err;
    }

    nvs_handle_t handle;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle) != ESP_OK) {
        ESP_LOGI(TAG, "no saved settings; using the compiled-in defaults");
        return ESP_OK;
    }

    // Static for the same reason as the scan buffer in csi_wifi.c: this runs on app_main's stack,
    // which startup is already using close to its limit.
    static stored_t stored;
    size_t length = sizeof(stored);
    err = nvs_get_blob(handle, NVS_KEY, &stored, &length);
    nvs_close(handle);

    if (err != ESP_OK || length != sizeof(stored)) {
        ESP_LOGI(TAG, "no saved settings; using the compiled-in defaults");
        return ESP_OK;
    }
    if (stored.version != SETTINGS_VERSION) {
        ESP_LOGW(TAG, "saved settings are version %u, this firmware wants %d — discarding",
                 (unsigned)stored.version, SETTINGS_VERSION);
        return ESP_OK;
    }

    s_settings = stored.settings;
    s_provisioned = true;
    ESP_LOGI(TAG, "loaded saved settings: ssid \"%s\", server %s:%u, node %u, %u Hz",
             s_settings.ssid, s_settings.server_host, (unsigned)s_settings.server_port,
             (unsigned)s_settings.node_id, (unsigned)s_settings.probe_rate_hz);
    if (s_settings.lock_bssid[0] != '\0') {
        ESP_LOGI(TAG, "locked to %s", s_settings.lock_bssid);
    }
    return ESP_OK;
}

const csi_settings_t *csi_settings(void) { return &s_settings; }

bool csi_settings_provisioned(void) { return s_provisioned; }

esp_err_t csi_settings_save(const csi_settings_t *settings) {
    stored_t stored = {.version = SETTINGS_VERSION, .settings = *settings};

    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(handle, NVS_KEY, &stored, sizeof(stored));
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);

    if (err == ESP_OK) {
        s_settings = *settings;
        s_provisioned = true;
        ESP_LOGI(TAG, "saved settings: ssid \"%s\", server %s:%u", s_settings.ssid,
                 s_settings.server_host, (unsigned)s_settings.server_port);
    }
    return err;
}

esp_err_t csi_settings_clear(void) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_erase_key(handle, NVS_KEY);
    if (err == ESP_OK || err == ESP_ERR_NVS_NOT_FOUND) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    s_provisioned = false;
    return err;
}

const char *csi_settings_validate(const csi_settings_t *settings) {
    if (settings->ssid[0] == '\0') {
        return "SSID is required";
    }
    // WPA2 needs eight characters. An open network is legitimate, so the empty string passes;
    // anything between one and seven is a typo that would fail at association time with an error
    // indistinguishable from a wrong password.
    const size_t password_length = strlen(settings->password);
    if (password_length > 0 && password_length < 8) {
        return "password must be empty (open network) or at least 8 characters";
    }

    uint8_t scratch[6];
    if (settings->lock_bssid[0] != '\0' && !csi_wifi_parse_mac(settings->lock_bssid, scratch)) {
        return "BSSID must look like aa:bb:cc:dd:ee:ff";
    }

    // An address rather than a hostname, for the same reason the Kconfig help gives: there is no
    // operator standing by when DNS fails on a node behind a sofa.
    unsigned octets[4];
    if (sscanf(settings->server_host, "%u.%u.%u.%u", &octets[0], &octets[1], &octets[2],
               &octets[3]) != 4) {
        return "server must be an IPv4 address, not a hostname";
    }
    for (int i = 0; i < 4; i++) {
        if (octets[i] > 255) {
            return "server address has an octet above 255";
        }
    }

    if (settings->server_port == 0) {
        return "server port must be 1-65535";
    }
    if (settings->node_id < 1 || settings->node_id > 254) {
        return "node ID must be 1-254 (0 and 255 are reserved)";
    }
    // The upper bound is the Kconfig range. The lower bound matters more than it looks: at 1 Hz
    // nothing downstream works, and the number is easy to mistype by a factor of a hundred.
    if (settings->probe_rate_hz < 1 || settings->probe_rate_hz > 500) {
        return "probe rate must be 1-500 Hz";
    }
    return NULL;
}
