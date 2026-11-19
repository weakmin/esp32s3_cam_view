> 🚧 **开发中** —— 本项目是「古明地觉 cos」相关装置的一部分，仍在持续开发中，电路、固件与文档可能随时调整。

---
# esp32s3_cam_view — 摄像头实时画面（USB 直传）

OV2640 → JPEG → ESP32-S3 内置 USB Serial/JTAG（CDC）→ 电脑窗口显示。
**不需要 WiFi、不需要额外 USB-UART 芯片、不需要改接线**，插上 USB 就能看。

## 1. 硬件

- ESP32-S3 板子（USB 口直接接电脑，Windows 免驱）
- OV2640 摄像头模组，接线与 ESP32-S3-EYE / `esp32s3_face_follow` 一致：

| 信号 | GPIO | 信号 | GPIO |
|---|---|---|---|
| XCLK | 15 | D0 | 11 |
| PCLK | 13 | D1 | 9 |
| VSYNC | 6 | D2 | 8 |
| HREF | 7 | D3 | 10 |
| SDA (SIOD) | 4 | D4 | 12 |
| SCL (SIOC) | 5 | D5 | 18 |
| D7 | 16 | D6 | 17 |

- PSRAM 按 Octal 8MB 配置（N8R8 / S3-EYE）。若是 Quad PSRAM 的板子，
  把 `sdkconfig.defaults` 里 `CONFIG_SPIRAM_MODE_OCT` 换成 `CONFIG_SPIRAM_MODE_QUAD`。

## 2. 编译烧写

环境（本机 IDF 装在 `C:\esp`）：

```powershell
$env:IDF_PATH='C:\esp\esp-idf'
$env:IDF_TOOLS_PATH='C:\esp\espressif'
$env:IDF_PYTHON_ENV_PATH='C:\esp\espressif\python_env\idf5.3_py3.10_env'
. C:\esp\esp-idf\export.ps1        # 或直接跑下面的命令，export 会补全 PATH

cd D:\work\esp32s3_cam_view          # 纯 ASCII 源码路径（工作目录里的 esp32s3_cam_view 是指向它的 junction）
idf.py set-target esp32s3
idf.py -p COM10 flash
```

> 板子里如果跑的是**出厂固件**（USB 被固件接管），esptool 可能无法自动复位进入下载模式。
> 解决办法：**按住 BOOT 键 → 点一下 RST/EN（或拔插一次 USB）→ 松开 BOOT**，
> 此时串口会换成新号（如 COM10，PID 1001），再执行 `idf.py -p COM10 flash`。
> 烧录后如果芯片停在 `waiting for download`，按一下 RST 或拔插 USB 让它正常启动。

### 本板现在的全自动烧录流程（不用按键）

固件用内置 USB-JTAG 做 console，所以 `usb_reset` 能直接进下载模式：

```powershell
# 1) 自动进下载模式
python C:\esp\esp-idf\components\esptool_py\esptool\esptool.py --chip esp32s3 -p COM10 --before usb_reset --after no_reset chip_id
# 2) 烧录（保持在 bootloader）
cd D:\work\esp32s3_cam_view\build
python C:\esp\esp-idf\components\esptool_py\esptool\esptool.py --chip esp32s3 -p COM10 -b 460800 --before no_reset --after no_reset write_flash --flash_mode dio --flash_size 2MB --flash_freq 80m 0x0 bootloader\bootloader.bin 0x8000 partition_table\partition-table.bin 0x10000 esp32s3_cam_view.bin
# 3) 用 RTS 软复位让它跑起来（USB-JTAG 的 hard_reset 不管用）
py -3.10 D:\work\esp32s3_cam_view\tools\reset_chip.py COM10
```

## 2.5 关键坑：USB-JTAG 的 console 输出会丢字节

`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG` 的 legacy console 是**非阻塞**的：TX FIFO 一满就
**静默丢弃**数据。用它发 JPEG 会得到「大块品红 + 水平撕裂」的残帧（PIL 还能解出图像，所以
看起来像是“摄像头坏了”）。

修复：装 `usb_serial_jtag` 驱动，改用 `usb_serial_jtag_write_bytes()`（阻塞，带超时）发帧：

```c
usb_serial_jtag_driver_config_t usb_cfg = { .tx_buffer_size = 32768, .rx_buffer_size = 1024 };
usb_serial_jtag_driver_install(&usb_cfg);
...
usb_serial_jtag_write_bytes(s_tx, len + 8, portMAX_DELAY);
```

另外摄像头帧缓冲用 `CAMERA_FB_IN_DRAM`（内部 RAM，QVGA JPEG 才几 KB，够用），
避免 PSRAM DMA 的额外变量。

## 3. 看画面

```powershell
py -3.10 D:\work\esp32s3_cam_view\tools\camview_pc.py --port COM10
```

会弹出窗口实时显示，同时把最新一帧写到 `--save-dir` 指定的 `latest.jpg`。
参数：`--no-window` 只保存不弹窗，`--save-dir` 换保存目录，`--keep N` 循环保留 N 张历史帧。

## 4. 帧协议

```
4B  "JPG0"        帧头
4B  uint32 LE     负载长度
NB  JPEG 数据     FFD8 ... FFD9
```

PC 端靠 `JPG0` 重新同步，遇到日志/杂字节会自动跳过。

## 5. 调参

`main/cam_view.c` 顶部：

| 宏 | 含义 |
|---|---|
| `FRAME_SIZE` | `FRAMESIZE_QVGA`(320×240) / `FRAMESIZE_VGA`(640×480) / `FRAMESIZE_SVGA`(800×600) |
| `JPEG_QUALITY` | 0–63，越小越清晰、越大越省带宽 |
| `FPS_LOG_S` | 串口打印帧率的间隔（写到 stderr，不会污染帧） |

USB-JTAG CDC 实测带宽约 1–2 MB/s，QVGA JPEG 约 15 KB/帧，帧率有余量；
如果要 640×480 且高码率，可考虑改用 `fb_count=3`。
