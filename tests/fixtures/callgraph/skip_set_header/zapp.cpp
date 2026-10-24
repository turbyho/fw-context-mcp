#include "driver.h"

int Driver::send(int socket_id) { return socket_id; }

bool shutdown(Driver& driver) {
    return driver.off();
}
