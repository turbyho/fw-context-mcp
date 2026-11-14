#include "sensor.h"

static SensorState state = SENSOR_IDLE;
static int32_t last_value;

void sensor_init(void)
{
    state = SENSOR_IDLE;
    last_value = 0;
}

int32_t sensor_read(void)
{
    state = SENSOR_BUSY;
    last_value += 1;
    state = SENSOR_IDLE;
    return last_value;
}

SensorState sensor_state(void)
{
    return state;
}
