"""验证调试命令通道：发不同参数，各抓一帧对比。"""
import os
import sys
import time

import serial

MAGIC = b"JPG0"
port = sys.argv[1] if len(sys.argv) > 1 else "COM10"
out = sys.argv[2] if len(sys.argv) > 2 else r"D:\work\cmd_frames"
os.makedirs(out, exist_ok=True)

s = serial.Serial(port, 921600, timeout=0.05)
s.setDTR(False)
s.setRTS(False)


def send(text: str) -> None:
    s.write((text + "\n").encode())
    print("->", text)


def grab(seconds: float, name: str) -> str | None:
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
    path = os.path.join(out, name + ".jpg")
    if last:
        with open(path, "wb") as f:
            f.write(last)
        print("saved", path, len(last))
        return path
    print("no frame for", name)
    return None


grab(1.5, "color_default")
send("effect 2")          # 黑白
grab(1.5, "effect_bw")
send("effect 0")
send("bright 2")
grab(1.5, "bright_hi")
send("bright 0")
send("contrast 2")
grab(1.5, "contrast_hi")
send("contrast 0")
s.close()
print("done")
