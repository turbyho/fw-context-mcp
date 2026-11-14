#include <Arduino.h>

#include "sensor.h"

class Blinker {
public:
    explicit Blinker(uint8_t pin) : pin_(pin) {}
    void begin() { pinMode(pin_, OUTPUT); }
    void toggle() { digitalWrite(pin_, !digitalRead(pin_)); }

private:
    uint8_t pin_;
};

static Blinker blinker(8);

void setup()
{
    sensor_init();
    blinker.begin();
}

void loop()
{
    if (sensor_read() > 0) {
        blinker.toggle();
    }
    delay(500);
}
