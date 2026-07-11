cmake_minimum_required(VERSION 3.16)
project(fwctx_test_cmake C)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_executable(test_app src/main.c)
