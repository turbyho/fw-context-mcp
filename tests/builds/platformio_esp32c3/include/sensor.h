#ifndef SENSOR_H
#define SENSOR_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    SENSOR_IDLE,
    SENSOR_BUSY,
    SENSOR_ERROR,
} SensorState;

void sensor_init(void);
int32_t sensor_read(void);
SensorState sensor_state(void);

#ifdef __cplusplus
}
#endif

#endif
