/* C++ code of the test project, with the Arduino core of the framework. */
#include <Arduino.h>

#include "blink.h"

class Led {
public:
    explicit Led(int pin) : pin_(pin) {}
    void show(BlinkState state) { digitalWrite(pin_, state == BLINK_ON ? HIGH : LOW); }

private:
    int pin_;
};

static Led led(LED_BUILTIN);
static BlinkState state = BLINK_OFF;

void setup() {
    pinMode(LED_BUILTIN, OUTPUT);
    blink_init();
}

void loop() {
    state = blink_next(state);
    led.show(state);
    delay(500);
}
