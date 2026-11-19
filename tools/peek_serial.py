"""偷看一眼串口当前输出，用来判断板子里跑的是什么固件。"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM8"
try:
    s = serial.Serial(port, 115200, timeout=0.2)
except Exception as e:  # noqa: BLE001
    print("ERR", e)
    sys.exit(1)

t0 = time.time()
buf = b""
while time.time() - t0 < 3:
    buf += s.read(4096)
s.close()

print("bytes:", len(buf))
print("hex  :", buf[:80].hex(" "))
print("text :")
print(buf[:1500].decode("utf-8", "replace"))
