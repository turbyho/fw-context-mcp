/**
 * Arduino hello-world test project for fw-context indexing tests.
 *
 * Board: arduino:avr:uno
 * Build: arduino-cli compile --fqbn arduino:avr:uno
 */

typedef enum {
    OFF = 0,
    ON = 1,
    BLINK = 2,
} LedState;

struct LedConfig {
    int pin;
    LedState state;
    unsigned long interval;
};

static LedConfig g_led = {13, BLINK, 500};

void set_led_state(LedState state) {
    g_led.state = state;
}

void set_led_interval(unsigned long ms) {
    g_led.interval = ms;
}

void update_led(unsigned long now_ms) {
    static unsigned long last_toggle = 0;
    if (g_led.state != BLINK) return;
    if (now_ms - last_toggle >= g_led.interval) {
        last_toggle = now_ms;
        // digitalWrite would be here in real Arduino code
    }
}

void setup() {
    set_led_state(BLINK);
    set_led_interval(500);
}

void loop() {
    static int counter = 0;
    update_led((unsigned long)counter * 10);
    counter++;
    delay(10);
}
