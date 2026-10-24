#include "radio.h"

int Radio::configure() {
    return ::configure(3) ? 1 : 0;
}
