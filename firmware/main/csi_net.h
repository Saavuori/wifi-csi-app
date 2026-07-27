#pragma once

#include <stdint.h>

#include "esp_err.h"

#include "csi_ring.h"

typedef struct {
    uint32_t sent;
    uint32_t bytes;
    uint32_t send_errors;
    uint32_t queued;
} csi_net_stats_t;

// Starts the packer/sender task pinned to Core 1. `ring` must outlive it.
esp_err_t csi_net_start(csi_ring_t *ring, const char *host, uint16_t port);

void csi_net_get_stats(csi_net_stats_t *out);
