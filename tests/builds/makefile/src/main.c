/**
 * Makefile hello-world test project for fw-context indexing tests.
 *
 * Indexed via: fw-context index --build  (uses compiledb)
 */

#include <stdio.h>
#include <string.h>

typedef struct {
    int id;
    char name[32];
} Device;

static Device g_devices[4];

int register_device(int id, const char* name) {
    for (int i = 0; i < 4; i++) {
        if (g_devices[i].id == 0) {
            g_devices[i].id = id;
            strncpy(g_devices[i].name, name, 31);
            return i;
        }
    }
    return -1;
}

int find_device(int id) {
    for (int i = 0; i < 4; i++) {
        if (g_devices[i].id == id) return i;
    }
    return -1;
}

int main(void) {
    register_device(1, "sensor");
    register_device(2, "actuator");
    printf("device 1 at index %d\n", find_device(1));
    return 0;
}
