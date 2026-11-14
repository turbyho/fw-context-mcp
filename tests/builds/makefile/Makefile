# Makefile test project for the fw-context indexing tests
# (tests/test_builder_projects.py).  The test gives CC, AR and CPU_FLAGS of
# a test SDK.  A static library and no program: a program of a bare-metal
# target needs a linker script and startup code.
CC = gcc
AR = ar
# The flags of the CPU of the target (-mcpu=..., -mthumb).
CPU_FLAGS =
CFLAGS = -Wall -Wextra -std=c11 -MMD -MP $(CPU_FLAGS)
# The output directory.  fw-context gives it with out_dir_var = "BUILD_DIR".
BUILD_DIR ?= .
SRC = src/main.c
# The objects in the output directory itself: fw-context makes that
# directory, and a recipe with `mkdir -p` would need a POSIX shell (make on
# Windows runs a recipe with cmd.exe when no sh.exe is on PATH).
OBJ = $(addprefix $(BUILD_DIR)/,$(notdir $(SRC:.c=.o)))
DEP = $(OBJ:.o=.d)
TARGET = $(BUILD_DIR)/libtest_app.a

.PHONY: all
all: $(TARGET)

$(TARGET): $(OBJ)
	$(AR) rcs $@ $^

$(BUILD_DIR)/%.o: src/%.c
	$(CC) $(CFLAGS) -c -o $@ $<

-include $(DEP)

.PHONY: clean
clean:
	rm -f $(OBJ) $(DEP) $(TARGET)
