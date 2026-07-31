#pragma once

// Runtime settings, in NVS, with the menuconfig values as the defaults.
//
// Why this exists: the BSSID lock is the one setting you cannot know before the node has been
// somewhere. It is chosen by reading a scan from where the board actually sits, and choosing
// again is the normal way to move the sensing line to a different doorway. Compiling it in makes
// that a rebuild-and-reflash cycle, which needs a toolchain, a cable, and the download-mode
// dance — for a value the device itself is best placed to offer a menu of.
//
// The Kconfig symbols stay, and stay meaningful: they are the values a freshly flashed board
// starts with. NVS only overrides them once something has been saved through the portal.

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

// Sizes match the 802.11 limits the WiFi driver enforces, plus the terminator, so a value that
// fits here always fits wifi_config_t and the truncation cannot happen later where it would be
// harder to explain.
#define CSI_SSID_MAX 33      // 32 + NUL
#define CSI_PASSWORD_MAX 65  // 64 + NUL
#define CSI_MAC_TEXT_MAX 18  // "aa:bb:cc:dd:ee:ff" + NUL
#define CSI_HOST_MAX 16      // "255.255.255.255" + NUL

typedef struct {
    char ssid[CSI_SSID_MAX];
    char password[CSI_PASSWORD_MAX];
    // Empty means "do not lock", which is the correct default for a single-AP network and the
    // wrong one for a mesh. csi_wifi.c warns about it at boot rather than deciding for you.
    char lock_bssid[CSI_MAC_TEXT_MAX];
    char server_host[CSI_HOST_MAX];
    uint16_t server_port;
    uint16_t probe_rate_hz;
    uint8_t node_id;
} csi_settings_t;

// Loads NVS over the compiled-in defaults. Call once, early, before anything reads settings.
// Never fails in a way worth handling: a missing or corrupt namespace yields the defaults, which
// is precisely the first-boot case.
esp_err_t csi_settings_load(void);

// The live settings. Valid after csi_settings_load(); returns the defaults before it.
const csi_settings_t *csi_settings(void);

// Writes to NVS. The caller is expected to reboot afterwards: the WiFi driver, the capture
// filter and the probe task all read their configuration once at startup, and re-plumbing them
// to change underneath a running capture would add exactly the kind of mid-session
// discontinuity the link epoch exists to make visible.
esp_err_t csi_settings_save(const csi_settings_t *settings);

// True once something has been saved through the portal. False on a freshly flashed board, which
// is what sends it to the portal instead of trying to join the placeholder SSID from Kconfig.
bool csi_settings_provisioned(void);

// Forgets the saved settings, so the next boot comes up in the portal.
esp_err_t csi_settings_clear(void);

// Validates a candidate before it is saved. Returns NULL when it is usable, or a short reason
// suitable for showing in the portal. Checked on the device rather than only in the browser
// because the failure mode being prevented — a board that reboots into a network it cannot join
// and has no console attached — costs a walk to wherever the node is.
const char *csi_settings_validate(const csi_settings_t *settings);
