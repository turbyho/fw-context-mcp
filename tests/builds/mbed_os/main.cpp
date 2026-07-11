/**
 * Mbed OS hello-world test project for fw-context indexing tests.
 *
 * Target: NUCLEO_F429ZI (STM32F429)
 * Build: fw-context index --build
 */

#include "mbed.h"

static DigitalOut led1(LED1);
static DigitalOut led2(LED2);

typedef enum {
    BLINK_SLOW = 0,
    BLINK_FAST = 1,
    BLINK_OFF = 2,
} BlinkMode;

typedef struct {
    BlinkMode mode;
    int interval_ms;
    int count;
} BlinkConfig;

static BlinkConfig g_config = {BLINK_SLOW, 500, 0};

void blink_configure(BlinkMode mode, int interval_ms) {
    g_config.mode = mode;
    g_config.interval_ms = interval_ms;
}

void blink_update(void) {
    if (g_config.mode == BLINK_OFF) {
        led1 = 0;
        led2 = 0;
        return;
    }
    led1 = !led1;
    led2 = !led1;
    g_config.count++;
}

int main(void) {
    blink_configure(BLINK_SLOW, 500);
    while (1) {
        blink_update();
        thread_sleep_for(g_config.interval_ms);
    }
}
