# ESP-IDF hello-world test project for fw-context indexing tests.
#
# Build: idf.py set-target esp32 && idf.py build
# Requires: ESP-IDF installed and IDF_PATH set
#
# This is a DETECTION-ONLY fixture — idf.py is not installed on this system.

cmake_minimum_required(VERSION 3.16.0)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(fwctx_test_esp_idf)
