idf_component_register(
    SRCS
        "main.c"
        "csi_capture.c"
        "csi_net.c"
        "csi_portal.c"
        "csi_probe.c"
        "csi_ring.c"
        "csi_settings.c"
        "csi_tx.c"
        "csi_wifi.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event esp_timer nvs_flash lwip esp_http_server json
             esp_driver_gpio
    # The setup page, linked in as a blob rather than pasted into a C string literal so it stays
    # editable and diffable as HTML.
    EMBED_TXTFILES "portal.html"
)
