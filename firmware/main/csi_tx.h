#pragma once

#include "esp_err.h"

// Starts the fixed-rate transmit task. Transmitter role only.
esp_err_t csi_tx_start(void);
