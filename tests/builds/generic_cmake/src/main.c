/**
 * Generic CMake hello-world test project for fw-context indexing tests.
 */

#include <stdio.h>

int factorial(int n) {
    if (n <= 1) return 1;
    return n * factorial(n - 1);
}

int main(int argc, char** argv) {
    printf("factorial(5) = %d\n", factorial(5));
    return 0;
}
