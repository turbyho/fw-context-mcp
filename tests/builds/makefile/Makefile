CC = gcc
CFLAGS = -Wall -Wextra -std=c11 -MMD -MP
SRC = src/main.c
OBJ = $(SRC:.c=.o)
DEP = $(OBJ:.o=.d)
TARGET = test_app

.PHONY: all
all: $(TARGET)

$(TARGET): $(OBJ)
	$(CC) $(CFLAGS) -o $@ $^

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

-include $(DEP)

.PHONY: clean
clean:
	rm -f $(OBJ) $(DEP) $(TARGET)
