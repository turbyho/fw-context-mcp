CC = gcc
CFLAGS = -Wall -Wextra -std=c11 -MMD -MP
# The output directory.  fw-context gives it with out_dir_var = "BUILD_DIR".
BUILD_DIR ?= .
SRC = src/main.c
OBJ = $(SRC:%.c=$(BUILD_DIR)/%.o)
DEP = $(OBJ:.o=.d)
TARGET = $(BUILD_DIR)/test_app

.PHONY: all
all: $(TARGET)

$(TARGET): $(OBJ)
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD_DIR)/%.o: %.c
	mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c -o $@ $<

-include $(DEP)

.PHONY: clean
clean:
	rm -f $(OBJ) $(DEP) $(TARGET)
