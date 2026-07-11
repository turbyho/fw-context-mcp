# Zephyr hello-world test project for fw-context indexing tests.
#
# Build: west build -b nrf52840dk/nrf52840
# Requires: ZEPHYR_BASE=~/ncs/v3.2.3/zephyr
#           ZEPHYR_SDK_INSTALL_DIR=~/ncs/toolchains/2ac5840438/opt/zephyr-sdk
#           PATH += ~/ncs/toolchains/.../arm-zephyr-eabi/bin

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(fwctx_test_zephyr)

target_sources(app PRIVATE src/main.c)
