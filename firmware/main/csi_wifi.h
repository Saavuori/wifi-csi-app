#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

// Joins the configured access point and blocks until an IP is assigned (or the retry budget is
// spent). Both node roles do this: the receiver needs an IP to reach the server, and the
// transmitter needs one to be worth measuring.
esp_err_t csi_wifi_start_sta(void);

// Puts the radio in promiscuous mode on the current channel so CSI is reported for frames the
// node was not the destination of — which is how a receiver measures the transmitter's link
// directly rather than the access point's.
esp_err_t csi_wifi_start_promiscuous(void);

// Parses "aa:bb:cc:dd:ee:ff" into six bytes.
bool csi_wifi_parse_mac(const char *text, uint8_t out[6]);

uint8_t csi_wifi_channel(void);
