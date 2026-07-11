/**
 * PlatformIO hello-world test project for fw-context indexing tests.
 *
 * Board: disco_l475vg_iot01a (STM32L4)
 * Framework: stm32cube
 */

#include <stdint.h>

typedef enum {
    SENSOR_IDLE = 0,
    SENSOR_READING = 1,
    SENSOR_ERROR = 2,
} SensorState;

typedef struct {
    uint32_t address;
    uint8_t channel;
    SensorState state;
    int16_t last_value;
} Sensor;

static Sensor g_sensor;

int sensor_init(uint32_t i2c_address, uint8_t channel) {
    g_sensor.address = i2c_address;
    g_sensor.channel = channel;
    g_sensor.state = SENSOR_IDLE;
    g_sensor.last_value = 0;
    return 0;
}

int sensor_read(void) {
    if (g_sensor.state == SENSOR_ERROR) return -1;
    g_sensor.state = SENSOR_READING;
    g_sensor.last_value = 42;  // dummy
    g_sensor.state = SENSOR_IDLE;
    return g_sensor.last_value;
}

int main(void) {
    sensor_init(0x48, 1);
    int val;
    while (1) {
        val = sensor_read();
        if (val < 0) break;
    }
    return 0;
}
