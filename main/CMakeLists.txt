idf_component_register(SRCS "cam_view.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp32-camera
                       PRIV_REQUIRES esp_timer driver esp_driver_usb_serial_jtag)
