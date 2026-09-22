CC ?= gcc
CFLAGS ?= -O2 -fPIC -Wall -Wextra -Werror
LDFLAGS ?= -shared -pthread -Wl,-soname,libuptech.so

.PHONY: all clean

all: libuptech.so

libuptech.so: libuptech.c
	$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS)

clean:
	rm -f libuptech.so
