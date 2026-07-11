/**
 * Bare-metal hello-world test project for fw-context indexing tests.
 *
 * Indexed via manual/bare mode: fw-context index --source-dirs src
 */

#include "lib.h"

int main(void) {
    uart_init(115200);
    int result = compute_checksum("hello", 5);
    return result;
}
