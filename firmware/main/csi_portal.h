#pragma once

#include <stdbool.h>

#include "esp_err.h"

// The provisioning portal: a SoftAP and a small HTTP server that writes csi_settings and reboots.
//
// It runs *instead of* capture, never alongside it. That is a deliberate constraint rather than
// a simplification: this firmware's whole premise is that Core 0 belongs to the WiFi driver and
// Core 1 to the packer and the prober, and an HTTP server accepting connections at unpredictable
// times is exactly the sort of work that turns a steady 100 Hz stream into a bursty one. A node
// that is being configured is not measuring anything, so there is nothing to disturb.
//
// Blocks forever. The only way out is the reboot that saving triggers.
esp_err_t csi_portal_run(const char *reason);

// Serves the same page and API on the network the node joined, at its own address, while it goes
// on measuring. Returns immediately.
//
// This is the everyday way in; csi_portal_run() is the recovery path for when the node is not on
// a network you can reach. The cost of leaving it running is one task blocked on a listening
// socket and about 12 KB — it is not in the CSI callback and not in the packer, and it does real
// work only while the page is actually open. Pinned to Core 1 below the sender and the prober so
// that when it does, they still win.
//
// Saving from here reboots, exactly as it does from the setup network: the WiFi driver, the
// capture filter and the prober all read their configuration once at startup.
esp_err_t csi_portal_start_station(void);

// Watches the boot button for a few seconds and returns true if it is held down during that
// window. This is the way back for a node configured onto a network that no longer exists, where
// nothing over the air can reach it any more.
//
// Press it *after* the board is running, not through the reset. On the usual DevKit boards this
// is GPIO0, which the ROM bootloader also uses to select serial download mode: held through
// reset, the board stops in the bootloader and this code never runs.
//
// Blocks for the length of the window, so it is called once, before anything else starts.
bool csi_portal_button_held(void);
