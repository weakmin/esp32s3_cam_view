"""通过 USB-JTAG CDC 的 RTS/DTR 软复位芯片，让它从 bootloader 启动应用。"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM10"
s = serial.Serial(port, 115200, timeout=0.2)
s.setDTR(False)   # IO0=1 -> 正常启动
s.setRTS(True)    # EN 拉低 -> 复位
time.sleep(0.15)
s.setRTS(False)   # 释放 EN
time.sleep(0.5)
s.close()
print("reset pulse sent")
