# Zephyr test project for the fw-context indexing tests (tests/test_sdk_projects.py).
#
# Build: west build -b nrf52840dk/nrf52840 --sysbuild
# The test SDKs (tests/sdk, `make test-sdk`) give Zephyr and the Zephyr SDK.
# The sysbuild has one image, this application: a second image (MCUboot)
# doubles the time of each index run.

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(fwctx_test_zephyr)

target_sources(app PRIVATE src/main.c)
