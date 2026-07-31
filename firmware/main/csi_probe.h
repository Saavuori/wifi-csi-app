#pragma once

#include <stdint.h>

#include "esp_err.h"

typedef struct {
    uint32_t probes;      // packets sent
    uint32_t replies;     // replies read back off the socket
    uint32_t send_errors; // sendto() failures, usually the link being down
} csi_probe_stats_t;

// Starts the fixed-rate probe task. Station role only. Call after the node has an IP: the
// default target is the DHCP gateway, which is not known before then.
esp_err_t csi_probe_start(void);

void csi_probe_get_stats(csi_probe_stats_t *out);
