# The toolchain file of a cross build for a bare-metal target.  CMake takes
# the compiler from CC and the flags from CFLAGS.  The compiler check of
# CMake makes a static library: a program needs a linker script.
set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)
