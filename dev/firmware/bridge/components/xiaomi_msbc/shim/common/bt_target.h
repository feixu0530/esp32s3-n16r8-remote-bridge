#pragma once

/*
 * Minimal platform shim for the unmodified OI SBC decoder sources.
 * The upstream files only need the decoder feature switch and integer booleans.
 */
#define SBC_DEC_INCLUDED 1
#define TRUE  1
#define FALSE 0
