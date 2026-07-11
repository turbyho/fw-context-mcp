#include "lib.h"

static int g_initialized = 0;

int uart_init(int baudrate) {
    if (baudrate <= 0) return -1;
    g_initialized = 1;
    return 0;
}

int compute_checksum(const char* data, int len) {
    if (!data || len <= 0) return -1;
    int sum = 0;
    for (int i = 0; i < len; i++) {
        sum += (unsigned char)data[i];
    }
    return sum & 0xFF;
}

void set_mode(OperationMode mode) {
    // stub
    (void)mode;
}
