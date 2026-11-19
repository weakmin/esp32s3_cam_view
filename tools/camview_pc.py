#!/usr/bin/env python3
"""
ESP32-S3 摄像头实时画面 + 调试面板（PC 端）

板子端每帧发：b"JPG0" + 4 字节小端长度 + JPEG 数据。
本脚本从串口解析图像，开窗口实时显示，右侧是可调摄像头参数的调试面板，
拖动滑块会通过同一条 USB 口把命令（如 "bright 1"）发给板子，立即生效。

用法：
    py -3.10 camview_pc.py --port COM10              # 窗口 + 调试面板
    py -3.10 camview_pc.py --port COM10 --no-window  # 只存 latest.jpg
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import threading
import time

import serial

MAGIC = b"JPG0"
MAX_FRAME = 2 * 1024 * 1024

# 与固件保持一致
GAIN_NAMES = ["2X", "4X", "8X", "16X", "32X", "64X", "128X"]
DEFAULTS = {
    "bright": 0,
    "contrast": 0,
    "saturation": 0,
    "ae": -1,
    "gain": 0,
    "quality": 12,
    "hflip": 0,
    "vflip": 0,
    "awb": 1,
}


class Reader(threading.Thread):
    """后台线程：读串口、拆帧；同时提供 send() 把命令写回板子。"""

    def __init__(self, port: str, out_dir: str, keep: int) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.out_dir = out_dir
        self.keep = keep
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.wlock = threading.Lock()
        self.ser: serial.Serial | None = None
        self.latest: bytes | None = None
        self.latest_id = 0
        self.frames = 0
        self.bytes = 0
        self.started = time.time()
        self.error: str | None = None
        self._saved = 0

    # ---- 对外接口 ----
    def get_latest(self) -> tuple[int, bytes | None]:
        with self.lock:
            return self.latest_id, self.latest

    def send(self, text: str) -> None:
        """把一行文本命令发给板子（如 "bright 1"）。"""
        ser = self.ser
        if ser is None or not ser.is_open:
            return
        try:
            with self.wlock:
                ser.write((text.strip() + "\n").encode("ascii", "ignore"))
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self.stop_flag.set()

    def fps(self) -> float:
        dt = time.time() - self.started
        return self.frames / dt if dt > 0 else 0.0

    # ---- 内部 ----
    def _store(self, payload: bytes) -> None:
        self._saved += 1
        if self.out_dir:
            latest = os.path.join(self.out_dir, "latest.jpg")
            tmp = latest + ".tmp"
            try:
                with open(tmp, "wb") as f:
                    f.write(payload)
                os.replace(tmp, latest)
            except OSError:
                pass
            if self.keep > 0 and (self._saved % 10 == 0):
                idx = self._saved % self.keep
                try:
                    with open(os.path.join(self.out_dir, f"frame_{idx:03d}.jpg"), "wb") as f:
                        f.write(payload)
                except OSError:
                    pass
        with self.lock:
            self.latest = payload
            self.latest_id += 1

    def run(self) -> None:
        try:
            ser = serial.Serial(self.port, 921600, timeout=0.05, write_timeout=1)
        except Exception as e:  # noqa: BLE001
            self.error = f"打开 {self.port} 失败: {e}"
            return
        self.ser = ser
        try:
            ser.reset_input_buffer()
        except Exception:  # noqa: BLE001
            pass

        buf = bytearray()
        try:
            while not self.stop_flag.is_set():
                chunk = ser.read(65536)
                if chunk:
                    buf += chunk
                    self.bytes += len(chunk)
                # 拆帧：找到 magic → 读长度 → 取 payload → 校验 JPEG 头尾
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
                    if ln < 4 or ln > MAX_FRAME:
                        del buf[:4]
                        continue
                    if len(buf) < 8 + ln:
                        break
                    payload = bytes(buf[8:8 + ln])
                    del buf[:8 + ln]
                    if payload[:2] != b"\xff\xd8":
                        continue
                    # 帧尾可能带 0x00 填充（DVP 对齐），裁到真正的 EOI
                    eoi = payload.rfind(b"\xff\xd9")
                    if eoi < 2:
                        continue
                    payload = payload[:eoi + 2]
                    self.frames += 1
                    self._store(payload)
        except Exception as e:  # noqa: BLE001
            self.error = f"串口读取出错: {e}"
        finally:
            ser.close()


class Window:
    def __init__(self, reader: Reader, title: str) -> None:
        import tkinter as tk

        self.tk = tk
        self.reader = reader
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg="#101012")
        self.root.geometry("980x640")
        self.root.minsize(760, 480)

        self._last_send: dict[str, float] = {}
        self._pending: dict[str, int] = {}
        self._vars: dict[str, object] = {}

        # 左侧：视频 + 状态栏
        left = tk.Frame(self.root, bg="#101012")
        left.pack(side="left", fill="both", expand=True)

        self.label = tk.Label(left, bg="#101012", fg="#ddd")
        self.label.pack(expand=True, fill="both")
        self.bar = tk.Label(left, anchor="w", bg="#0b0b0d", fg="#8ab4f8",
                            font=("Consolas", 10), padx=8, pady=4)
        self.bar.pack(fill="x")

        # 右侧：调试面板
        right = tk.Frame(self.root, bg="#0b0b0d", width=280)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self._build_panel(tk, right)

        self._last_id = -1
        self._photo = None
        self._img = None
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(15, self.tick)

    # ---------- 调试面板 ----------
    def _build_panel(self, tk, parent) -> None:
        tk.Label(parent, text="摄像头调试", bg="#0b0b0d", fg="#8ab4f8",
                 font=("Microsoft YaHei", 12, "bold")).pack(anchor="w", padx=10, pady=(10, 6))

        panel = tk.Frame(parent, bg="#0b0b0d")
        panel.pack(fill="x", padx=6)
        panel.grid_columnconfigure(0, weight=1)

        self._row = 0

        def slider(title: str, key: str, lo: int, hi: int, init: int) -> None:
            tk.Label(panel, text=title, bg="#0b0b0d", fg="#c8c8c8",
                     anchor="w", font=("Microsoft YaHei", 9)).grid(
                row=self._row, column=0, sticky="w", padx=4)
            self._row += 1
            var = tk.IntVar(value=init)
            sc = tk.Scale(panel, from_=lo, to=hi, orient="horizontal", variable=var,
                          bg="#0b0b0d", fg="#8ab4f8", troughcolor="#242428",
                          highlightthickness=0, length=240, sliderlength=18,
                          font=("Consolas", 9),
                          command=lambda v, k=key: self._on_slider(k, v))
            sc.grid(row=self._row, column=0, sticky="ew", padx=4, pady=(0, 4))
            # 松手/键盘松开时补发最终值：节流会丢掉拖动结束那一档
            sc.bind("<ButtonRelease-1>", lambda e, k=key: self._flush_slider(k))
            sc.bind("<KeyRelease>", lambda e, k=key: self._flush_slider(k))
            self._row += 1
            self._vars[key] = var

        slider("亮度  brightness", "bright", -2, 2, DEFAULTS["bright"])
        slider("对比度  contrast", "contrast", -2, 2, DEFAULTS["contrast"])
        slider("饱和度  saturation", "saturation", -2, 2, DEFAULTS["saturation"])
        slider("曝光补偿  ae_level", "ae", -2, 2, DEFAULTS["ae"])
        slider("增益上限  gainceiling (0=2X … 6=128X)", "gain", 0, 6, DEFAULTS["gain"])
        slider("JPEG 质量 (0 最清晰)", "quality", 0, 40, DEFAULTS["quality"])

        # 开关
        toggles = tk.Frame(parent, bg="#0b0b0d")
        toggles.pack(fill="x", padx=10, pady=(6, 0))
        for title, key, init in (
            ("水平翻转 hmirror", "hflip", DEFAULTS["hflip"]),
            ("垂直翻转 vflip", "vflip", DEFAULTS["vflip"]),
            ("自动白平衡 awb", "awb", DEFAULTS["awb"]),
        ):
            var = tk.IntVar(value=init)
            cb = tk.Checkbutton(toggles, text=title, variable=var, bg="#0b0b0d",
                                fg="#c8c8c8", selectcolor="#242428",
                                activebackground="#0b0b0d", activeforeground="#fff",
                                font=("Microsoft YaHei", 9),
                                command=lambda k=key, v=var: self._on_toggle(k, v.get()))
            cb.pack(anchor="w", pady=1)
            self._vars[key] = var

        # 按钮
        btns = tk.Frame(parent, bg="#0b0b0d")
        btns.pack(fill="x", padx=10, pady=(10, 4))
        self._btn(btns, "恢复默认", self._reset).pack(fill="x", pady=2)
        self._btn(btns, "保存当前帧", self._snapshot).pack(fill="x", pady=2)

        tk.Label(parent, text="滑块改动会立即下发给板子\n参数按 OV2640 范围", bg="#0b0b0d",
                 fg="#666666", font=("Microsoft YaHei", 8), justify="left").pack(
            anchor="w", padx=10, pady=(8, 0))

    def _btn(self, parent, text: str, cmd):
        return self.tk.Button(parent, text=text, command=cmd, bg="#1c1c22",
                              fg="#d0d0d0", activebackground="#2a2a32",
                              activeforeground="#fff", relief="flat", bd=0,
                              font=("Microsoft YaHei", 9), height=1, cursor="hand2")

    def _on_slider(self, key: str, value) -> None:
        v = int(round(float(value)))
        # 不论节流与否都记住最新值，松手时补发，避免最后值丢失
        self._pending[key] = v
        now = time.monotonic()
        if now - self._last_send.get(key, 0) >= 0.06:
            self._last_send[key] = now
            self.reader.send(f"{key} {v}")

    def _flush_slider(self, key: str) -> None:
        v = self._pending.pop(key, None)
        if v is not None:
            self._last_send[key] = time.monotonic()
            self.reader.send(f"{key} {v}")

    def _on_toggle(self, key: str, value: int) -> None:
        self.reader.send(f"{key} {1 if value else 0}")

    def _reset(self) -> None:
        for key, val in DEFAULTS.items():
            self.reader.send(f"{key} {val}")
            var = self._vars.get(key)
            if var is not None:
                try:
                    var.set(val)
                except Exception:  # noqa: BLE001
                    pass

    def _snapshot(self) -> None:
        fid, payload = self.reader.get_latest()
        if payload is None:
            return
        out = os.path.join(self.reader.out_dir or ".", f"snap_{time.strftime('%H%M%S')}.jpg")
        try:
            with open(out, "wb") as f:
                f.write(payload)
        except OSError:
            pass

    # ---------- 视频刷新 ----------
    def close(self) -> None:
        self.reader.stop()
        self.root.destroy()

    def tick(self) -> None:
        from PIL import Image, ImageTk

        fid, payload = self.reader.get_latest()
        if payload is not None and fid != self._last_id:
            self._last_id = fid
            try:
                img = Image.open(io.BytesIO(payload))
                img.load()
                self._img = img
                w, h = img.size
                box_w = max(self.label.winfo_width(), 320)
                box_h = max(self.label.winfo_height(), 240)
                scale = min(box_w / w, box_h / h)
                size = (max(1, int(w * scale)), max(1, int(h * scale)))
                disp = img.resize(size, Image.Resampling.BILINEAR)
                self._photo = ImageTk.PhotoImage(disp)
                self.label.configure(image=self._photo)
            except Exception:  # noqa: BLE001
                pass

        if self.reader.error:
            self.bar.configure(text=f"错误: {self.reader.error}")
        else:
            now = time.time()
            rate = (self.reader.bytes / (now - self.reader.started)) / 1024.0 if now > self.reader.started else 0
            dim = f"{self._img.size[0]}x{self._img.size[1]}" if self._img else "--"
            self.bar.configure(
                text=f"{self.reader.fps():5.1f} fps   {rate:7.1f} KB/s   帧 {self.reader.frames:6d}   {dim}   {self.reader.port}"
            )
        if not self.reader.stop_flag.is_set():
            self.root.after(15, self.tick)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser(description="ESP32-S3 USB 摄像头实时画面 + 调试面板")
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--save-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--keep", type=int, default=20, help="循环保留的帧数，0 表示不留历史帧")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--duration", type=float, default=0, help="运行秒数，0 表示一直跑")
    args = ap.parse_args()

    out_dir = args.save_dir
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    reader = Reader(args.port, out_dir, args.keep)
    reader.start()

    if args.no_window:
        t0 = time.time()
        try:
            while not reader.stop_flag.is_set() and not reader.error:
                if args.duration and time.time() - t0 > args.duration:
                    break
                time.sleep(0.5)
                print(f"\r{reader.fps():5.1f} fps  {reader.bytes/1024:8.0f} KB", end="", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            reader.stop()
            print()
        if reader.error:
            print("error:", reader.error)
            return 1
        print(f"frames={reader.frames}, latest saved to {os.path.join(out_dir, 'latest.jpg')}")
        return 0

    win = Window(reader, f"ESP32-S3 摄像头 — {args.port}")
    win.run()
    reader.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
