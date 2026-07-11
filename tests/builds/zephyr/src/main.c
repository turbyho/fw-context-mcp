/**
 * Zephyr RTOS hello-world test project for fw-context indexing tests.
 *
 * Board: nrf52840dk_nrf52840
 * Build: west build -b nrf52840dk_nrf52840
 * Requires: ZEPHYR_BASE=~/ncs/v3.2.3/zephyr
 *           ZEPHYR_SDK_INSTALL_DIR=~/ncs/toolchains/2ac5840438/opt/zephyr-sdk
 */

#include <zephyr/kernel.h>

typedef enum {
    MSG_IDLE = 0,
    MSG_SEND = 1,
    MSG_RECV = 2,
} MsgState;

struct MsgBuffer {
    uint8_t data[64];
    int len;
    MsgState state;
};

static struct MsgBuffer g_buffer;

int msg_init(void) {
    g_buffer.len = 0;
    g_buffer.state = MSG_IDLE;
    return 0;
}

int msg_send(const uint8_t* data, int len) {
    if (!data || len <= 0 || len > 64) return -1;
    for (int i = 0; i < len; i++) {
        g_buffer.data[i] = data[i];
    }
    g_buffer.len = len;
    g_buffer.state = MSG_SEND;
    return 0;
}

int msg_recv(uint8_t* buf, int max_len) {
    if (!buf || max_len <= 0) return -1;
    if (g_buffer.state != MSG_RECV && g_buffer.state != MSG_SEND) return -1;
    int n = g_buffer.len < max_len ? g_buffer.len : max_len;
    for (int i = 0; i < n; i++) {
        buf[i] = g_buffer.data[i];
    }
    g_buffer.state = MSG_IDLE;
    return n;
}

int main(void) {
    msg_init();
    uint8_t data[64];
    msg_send((const uint8_t*)"hello", 5);
    msg_recv(data, 64);
    return 0;
}
