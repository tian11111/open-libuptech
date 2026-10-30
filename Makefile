CC ?= gcc
CFLAGS ?= -O2 -fPIC -Wall -Wextra -Werror
LDFLAGS ?= -shared -pthread -Wl,-soname,libuptech.so
LDLIBS ?= -lm

.PHONY: all clean test

all: libuptech.so

libuptech.so: libuptech.c
	$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

test:
	python3 -m unittest discover -s tests -p test_fan_control.py -v
	python3 -m unittest discover -s tests -p test_mpu_cache.py -v
	python3 -m unittest discover -s tests -p test_uart_tx.py -v
	python3 -m unittest discover -s tests -p test_motor_gui.py -v

clean:
	rm -f libuptech.so
