#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

// Called from the WiFi event task every time the node associates, with the BSSID it associated
// with and the link epoch that association started. The station role uses this to re-arm the
// capture filter: without a BSSID lock a mesh network can move the node to a different access
// point, and a filter still pointing at the old one silently rejects every frame.
typedef void (*csi_wifi_link_cb_t)(const uint8_t bssid[6], uint8_t epoch);

// Register before calling csi_wifi_start_sta(), or the first association is missed.
void csi_wifi_set_link_cb(csi_wifi_link_cb_t cb);

// Joins the configured access point and blocks until an IP is assigned (or the retry budget is
// spent). Every node role does this: the station and receiver need an IP to reach the server,
// and the transmitter needs an association to be worth measuring.
//
// Scans first when CSI_SCAN_ON_BOOT is set, and locks to CSI_LOCK_BSSID when that is set.
esp_err_t csi_wifi_start_sta(void);

// Puts the radio in promiscuous mode on the current channel so CSI is reported for frames the
// node was not the destination of — which is how the *receiver* role measures a second node's
// link directly rather than the access point's. The station role does not use this: in plain
// station mode the driver already reports CSI for the frames the AP sends to us, which is the
// link we want, and sniffing the whole channel would only give the callback more to reject.
esp_err_t csi_wifi_start_promiscuous(void);

// Parses "aa:bb:cc:dd:ee:ff" into six bytes.
bool csi_wifi_parse_mac(const char *text, uint8_t out[6]);

uint8_t csi_wifi_channel(void);

// BSSID of the currently associated access point. False before the first association.
bool csi_wifi_bssid(uint8_t out[6]);

// Increments on every association. A change means the link is not the link it was — a roam, a
// reconnect, or the AP changing channel — and every baseline built from the old one is void.
// Travels in each frame so the server can drop the node's history at exactly the right point.
uint8_t csi_wifi_link_epoch(void);

// Disconnect events seen since boot. On a mesh this is the count of steering attempts the
// BSSID lock refused, which is worth watching: a number that keeps climbing means the network
// is still fighting the lock.
uint32_t csi_wifi_disconnects(void);

// The DHCP-assigned default gateway, network byte order, or 0 before the first lease.
uint32_t csi_wifi_gateway(void);

// This node's own address, network byte order, or 0 before the first lease. Only used to print
// the setup page's URL at boot — the node is found by address, not by mDNS, for the same reason
// the server is: one fewer thing to fail unattended.
uint32_t csi_wifi_ip(void);
