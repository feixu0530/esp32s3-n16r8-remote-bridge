# Vendored Apache-2.0 OI SBC/mSBC decoder

Source: ESP-IDF 6.1
`components/bt/host/bluedroid/external/sbc/decoder`

The decoder sources and headers are copied unmodified. This component adds only
`shim/common/bt_target.h`, a project-authored shim that selects the decoder
implementation and supplies the integer `TRUE`/`FALSE` values expected by the
upstream sources. The original Apache-2.0 license is in
`LICENSE-APACHE-2.0.txt`.
