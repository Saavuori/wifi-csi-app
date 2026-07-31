idf_component_register(
    SRCS
        "main.c"
        "csi_capture.c"
        "csi_net.c"
        "csi_probe.c"
        "csi_ring.c"
        "csi_tx.c"
        "csi_wifi.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event esp_timer nvs_flash lwip
)
