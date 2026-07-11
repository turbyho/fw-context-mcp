#ifndef LIB_H
#define LIB_H

typedef enum {
    MODE_IDLE = 0,
    MODE_ACTIVE = 1,
    MODE_SLEEP = 2,
} OperationMode;

struct Config {
    int baudrate;
    int mode;
    const char* name;
};

int uart_init(int baudrate);
int compute_checksum(const char* data, int len);
void set_mode(OperationMode mode);

#endif
