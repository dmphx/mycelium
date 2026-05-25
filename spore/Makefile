CC      = gcc
CFLAGS  = -shared -fPIC -O2 -Wall -Wextra -D_GNU_SOURCE -D_LARGEFILE64_SOURCE
LDFLAGS = -ldl -lpthread
TARGET  = mycelium_spore.so

.PHONY: all clean

all: $(TARGET)

$(TARGET): spore.c
	$(CC) $(CFLAGS) -o $(TARGET) spore.c $(LDFLAGS)
	strip $(TARGET)

clean:
	rm -f $(TARGET)
