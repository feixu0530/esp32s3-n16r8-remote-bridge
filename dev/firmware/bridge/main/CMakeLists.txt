idf_component_register(SRCS
    "app_main.c"
    "br_types.h"
    "br_proto.c"
    "br_proto.h"
    "br_ble_core.c"
    "br_hid.c"
    "br_hid.h"
    "br_hidmap.c"
    "br_hidmap.h"
    "br_mapping.c"
    "br_mapping.h"
    "br_gesture.c"
    "br_gesture.h"
    "br_logbuf.c"
    "br_logbuf.h"
    "br_ble_core.h"
    "br_usb.c"
    "br_usb.h"
    "br_voice.c"
    "br_voice.h"
    "br_led.c"
    "br_led.h"
    "br_uart.c"
    "br_uart.h"
    INCLUDE_DIRS "."
    REQUIRES bt nvs_flash esp_timer esp_driver_rmt esp_driver_uart
             driver esp_common esp_ringbuf esp_tinyusb tinyusb)

# The ESP-IDF TinyUSB wrapper exposes CDC/HID Kconfig switches but this
# project's custom composite descriptor also includes a UAC1 microphone.
#
# ★ 必须跳过 IDF 的「早期展开」轮 ★
#   IDF 为了收集各组件的依赖，会在一个独立的 CMake 脚本里把每个组件的
#   CMakeLists.txt 再 include 一遍（CMAKE_BUILD_EARLY_EXPANSION=1）。
#   那一轮里组件 target 还不存在，idf_component_get_property() 会直接
#   FATAL_ERROR: Failed to resolve component 'tinyusb'。
#   所以只在真正构建那一轮才去取 tinyusb 的 target。
if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    idf_component_get_property(tinyusb_lib tinyusb COMPONENT_LIB)
    set(BRIDGE_AUDIO_DEFS
        CFG_TUD_AUDIO=1
        CFG_TUD_AUDIO_ENABLE_EP_IN=1
        CFG_TUD_AUDIO_FUNC_1_SAMPLE_RATE=16000
        CFG_TUD_AUDIO_FUNC_1_N_BYTES_PER_SAMPLE_TX=2
        CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX=1
        CFG_TUD_AUDIO_EP_SZ_IN=64
        CFG_TUD_AUDIO_FUNC_1_EP_IN_SZ_MAX=64
        CFG_TUD_AUDIO_FUNC_1_EP_IN_SW_BUF_SZ=256)
    target_compile_definitions(${tinyusb_lib} PUBLIC ${BRIDGE_AUDIO_DEFS})
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ${BRIDGE_AUDIO_DEFS})
endif()
