"""测 brightness 命令是否真的改变画面：三个档位各抓一帧并算平均亮度。"""
import io
import os
import sys
import time

import serial
from PIL import Image

MAGIC = b"JPG0"
port = sys.argv[1] if len(sys.argv) > 1 else "COM10"
out = sys.argv[2] if len(sys.argv) > 2 else r"D:\work\bright_test"
os.makedirs(out, exist_ok=True)

s = serial.Serial(port, 921600, timeout=0.05)
s.setDTR(False)
s.setRTS(False)


def send(text: str) -> None:
    s.write((text + "\n").encode())
    time.sleep(0.25)
    print("->", text)


def grab(seconds: float) -> bytes | None:
    buf = bytearray()
    last = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        buf += s.read(65536)
        while True:
            i = buf.find(MAGIC)
            if i < 0:
                if len(buf) > 4:
                    del buf[:-4]
                break
            if i:
                del buf[:i]
            if len(buf) < 8:
                break
            ln = int.from_bytes(buf[4:8], "little")
            if ln < 4 or ln > 2 * 1024 * 1024:
                del buf[:4]
                continue
            if len(buf) < 8 + ln:
                break
            p = bytes(buf[8:8 + ln])
            del buf[:8 + ln]
            if p[:2] != b"\xff\xd8":
                continue
            eoi = p.rfind(b"\xff\xd9")
            if eoi > 2:
                last = p[:eoi + 2]
    return last


def mean_luma(jpg: bytes) -> float:
    img = Image.open(io.BytesIO(jpg)).convert("L")
    px = list(img.getdata())
    return sum(px) / len(px)


for level, name in ((-2, "bright_m2"), (2, "bright_p2"), (0, "bright_0")):
    send(f"bright {level}")
    jpg = grab(2.0)
    if jpg is None:
        print(name, "no frame")
        continue
    path = os.path.join(out, name + ".jpg")
    with open(path, "wb") as f:
        f.write(jpg)
    print(f"{name}: mean_luma={mean_luma(jpg):.1f}")

s.close()
