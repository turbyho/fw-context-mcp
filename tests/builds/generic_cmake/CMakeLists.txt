# Generic CMake test project for the fw-context indexing tests
# (tests/test_builder_projects.py).
#
# A cross build with the GCC of a test SDK: CMAKE_TOOLCHAIN_FILE names
# cmake/cross-gcc.cmake, and CC and CFLAGS give the compiler and the flags
# of the CPU.  A static library and no program: a program of a bare-metal
# target needs a linker script and startup code, and the test indexes the
# compilation alone.
cmake_minimum_required(VERSION 3.16)
project(fwctx_test_cmake C)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_library(test_app STATIC src/main.c)
