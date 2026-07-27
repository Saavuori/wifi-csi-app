#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "csi_ring.h"

typedef struct {
    uint32_t frames;   // frames that made it into the ring
    uint32_t dropped;  // frames the ring had no room for
    uint32_t filtered; // frames rejected by the source-MAC filter
    uint32_t oversize; // frames with more subcarriers than a slot holds
} csi_capture_stats_t;

// Installs the CSI callback and enables capture. `ring` must outlive it.
esp_err_t csi_capture_start(csi_ring_t *ring);
void csi_capture_stop(void);

// Only capture frames from this source MAC. Pass NULL to accept every frame.
// In promiscuous mode without a filter the node reports CSI for every packet in the air,
// including the neighbours' — which produces a fast, meaningless stream and a very confusing
// waterfall.
void csi_capture_set_peer(const uint8_t mac[6]);

// The task to wake when a frame lands in the ring. Set before starting capture; passing NULL
// leaves the consumer to poll.
void csi_capture_set_consumer(TaskHandle_t task);

void csi_capture_get_stats(csi_capture_stats_t *out);
