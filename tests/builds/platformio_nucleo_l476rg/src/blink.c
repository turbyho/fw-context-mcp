/* C code of the test project: the index reads it with the C driver of the toolchain. */
#include "blink.h"

static unsigned int blink_count;

void blink_init(void) {
    blink_count = 0;
}

BlinkState blink_next(BlinkState state) {
    blink_count++;
    return state == BLINK_ON ? BLINK_OFF : BLINK_ON;
}
