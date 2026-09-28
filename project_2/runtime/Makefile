# Container runtime build (provided). Needs Linux (namespaces + cgroup v2).
CC      := gcc
CFLAGS  := -std=gnu11 -Wall -Wextra -O2 -I.
SRC     ?= container.c          # override to build a different source: SRC=/path/to/container.c
OBJS    := main.o util.o net.o

container: $(OBJS) impl.o
	$(CC) $(CFLAGS) -o $@ $(OBJS) impl.o

impl.o: $(SRC) container.h
	$(CC) $(CFLAGS) -c -o $@ $<
main.o: main.c container.h
	$(CC) $(CFLAGS) -c -o $@ $<
util.o: util.c container.h
	$(CC) $(CFLAGS) -c -o $@ $<
net.o: net.c container.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f container $(OBJS) impl.o
.PHONY: clean
