set(srcs
    "srce/alloc.c"
    "srce/bitalloc-sbc.c"
    "srce/bitalloc.c"
    "srce/bitstream-decode.c"
    "srce/decoder-oina.c"
    "srce/decoder-private.c"
    "srce/decoder-sbc.c"
    "srce/dequant.c"
    "srce/framing-sbc.c"
    "srce/framing.c"
    "srce/oi_codec_version.c"
    "srce/synthesis-8-generated.c"
    "srce/synthesis-dct8.c"
    "srce/synthesis-sbc.c"
)

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "include" "shim"
)
