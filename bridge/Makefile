CC = clang
OUT = build/FCPBridge.dylib
SRC = FCPBridge.m

# Universal: FCP 11 ships arm64 + x86_64; NSInvocation keeps struct returns safe.
ARCHS = -arch arm64 -arch x86_64
FLAGS = -dynamiclib -undefined dynamic_lookup -O2 -fobjc-arc \
	-framework Foundation -framework AppKit -framework CoreMedia -framework WebKit

.PHONY: all clean

all: $(OUT)

$(OUT): $(SRC)
	mkdir -p build
	$(CC) $(ARCHS) $(FLAGS) -o $@ $(SRC)

clean:
	rm -rf build
