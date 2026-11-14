# ESP-IDF project of the extended set of the test SDKs (tests/sdk/versions.toml,
# marker extra_platforms): ESP32 with the version of ESP-IDF that
# versions.toml pins.  `python -m tests.sdk.provision --extra` installs it.
# idf.py builds the application and its bootloader.

cmake_minimum_required(VERSION 3.22)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(fwctx_test_esp_idf)
