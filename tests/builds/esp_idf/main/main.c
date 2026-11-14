/* The application of the ESP-IDF test project: a task that counts. */
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

typedef struct {
    unsigned int ticks;
    unsigned int period_ms;
} Counter;

static Counter counter = {0, 1000};

static void counter_step(Counter *state) {
    state->ticks++;
}

void app_main(void) {
    for (int round = 0; round < 3; round++) {
        counter_step(&counter);
        printf("tick %u\n", counter.ticks);
        vTaskDelay(pdMS_TO_TICKS(counter.period_ms));
    }
}
