/*
 * ESP32-S3 摄像头实时画面：OV2640 → JPEG → USB Serial/JTAG(CDC) → PC
 * ------------------------------------------------------------------
 * 帧格式（均为小端）：
 *   4B  magic "JPG0"        0x3047504A
 *   4B  payload length
 *   NB  JPEG 数据（以 FFD9 结尾）
 *
 * 说明：
 *   1. 摄像头 XCLK 占用 LEDC_TIMER_0 / CHANNEL_0，本工程不用舵机，无冲突。
 *   2. JPEG 由 OV2640 片内编码，CPU 只做搬运，所以 QVGA 下能跑很高帧率。
 *   3. USB Serial/JTAG 是 ESP32-S3 内置的，插线即用，不需要额外 USB-UART 芯片。
 *   4. 进入流模式后关闭日志，避免日志字节插进 JPEG 帧里。
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_camera.h"
#include "driver/usb_serial_jtag.h"

static const char *TAG = "camview";

/* ===== 接线（ESP32-S3-EYE 与 README 表一致） ===== */
#define CAM_PIN_PWDN   -1
#define CAM_PIN_RESET  -1
#define CAM_PIN_XCLK   15
#define CAM_PIN_SIOD   4
#define CAM_PIN_SIOC   5
#define CAM_PIN_D7     16
#define CAM_PIN_D6     17
#define CAM_PIN_D5     18
#define CAM_PIN_D4     12
#define CAM_PIN_D3     10
#define CAM_PIN_D2     8
#define CAM_PIN_D1     9
#define CAM_PIN_D0     11
#define CAM_PIN_VSYNC  6
#define CAM_PIN_HREF   7
#define CAM_PIN_PCLK   13

/* ===== 可调参数 ===== */
#define FRAME_SIZE  FRAMESIZE_QVGA   /* 320x240；改 FRAMESIZE_VGA 为 640x480 */
#define JPEG_QUALITY 12
#define FPS_LOG_S   3

#define FRAME_MAGIC 0x3047504Au      /* "JPG0" */

#define TX_CAP (64 * 1024)
#define FPS_TARGET 30          /* 限制发送速率：USB-JTAG 突发过快会丢字节 */
static uint8_t *s_tx;

static void stream_task(void *arg)
{
    uint32_t len;
    int64_t last_log = esp_timer_get_time();
    int frames = 0;
    int64_t bytes = 0;
    TickType_t last_wake = xTaskGetTickCount();

    while (true) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        if (fb->format == PIXFORMAT_JPEG) {
            len = fb->len;
            if (len > 0 && (size_t)len + 8 <= TX_CAP) {
                uint32_t magic = FRAME_MAGIC;
                memcpy(s_tx, &magic, 4);
                memcpy(s_tx + 4, &len, 4);
                memcpy(s_tx + 8, fb->buf, len);
                /* 用 USB-JTAG 驱动阻塞发送：console 的 legacy 路径在 FIFO 满时会丢字节 */
                size_t want = (size_t)len + 8;
                size_t off = 0;
                while (off < want) {
                    int n = usb_serial_jtag_write_bytes(s_tx + off, want - off, portMAX_DELAY);
                    if (n <= 0) {
                        break;
                    }
                    off += n;
                }
                frames++;
                bytes += len;
            }
        }
        esp_camera_fb_return(fb);

        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(1000 / FPS_TARGET));

        int64_t now = esp_timer_get_time();
        if (now - last_log >= (int64_t)FPS_LOG_S * 1000000) {
            /* 只在这一处输出，日志级别已关，仍用 printf 写到 stderr 避免污染帧 */
            fprintf(stderr, "[camview] %.1f fps, %.0f KB/s, frame %.0f KB\n",
                    frames * 1e6 / (now - last_log),
                    bytes * 1e6 / (now - last_log) / 1024.0,
                    frames ? bytes / (double)frames / 1024.0 : 0.0);
            frames = 0;
            bytes = 0;
            last_log = now;
        }
    }
}

/* ================= 调试命令通道（PC → 板子） =================
 * PC 端发文本行，如 "bright 1" / "contrast -1" / "gain 0" / "quality 8"，
 * 板子解析后直接调 sensor 寄存器，视频流不受影响。 */

static sensor_t *s_sensor;

static void apply_cmd(const char *name, int val)
{
    sensor_t *s = s_sensor;
    if (!s) {
        return;
    }
    if (!strcmp(name, "bright")) {
        s->set_brightness(s, val);
    } else if (!strcmp(name, "contrast")) {
        s->set_contrast(s, val);
    } else if (!strcmp(name, "saturation")) {
        s->set_saturation(s, val);
    } else if (!strcmp(name, "ae")) {
        s->set_ae_level(s, val);
    } else if (!strcmp(name, "gain")) {
        s->set_gainceiling(s, (gainceiling_t)val);
    } else if (!strcmp(name, "quality")) {
        s->set_quality(s, val);
    } else if (!strcmp(name, "hflip")) {
        s->set_hmirror(s, val);
    } else if (!strcmp(name, "vflip")) {
        s->set_vflip(s, val);
    } else if (!strcmp(name, "awb")) {
        s->set_whitebal(s, val);
    } else if (!strcmp(name, "awbgain")) {
        s->set_awb_gain(s, val);
    } else if (!strcmp(name, "effect")) {
        s->set_special_effect(s, val);
    }
}

static void rx_task(void *arg)
{
    char line[64];
    int n = 0;

    while (true) {
        char c;
        int r = usb_serial_jtag_read_bytes(&c, 1, pdMS_TO_TICKS(100));
        if (r <= 0) {
            continue;
        }
        if (c == '\n' || c == '\r') {
            if (n > 0) {
                line[n] = '\0';
                char name[20];
                int val = 0;
                if (sscanf(line, "%15s %d", name, &val) == 2) {
                    apply_cmd(name, val);
                }
                n = 0;
            }
        } else if (n < (int)sizeof(line) - 1) {
            line[n++] = c;
        }
    }
}

static esp_err_t camera_init(void)
{
    camera_config_t cfg = {
        .pin_pwdn     = CAM_PIN_PWDN,
        .pin_reset    = CAM_PIN_RESET,
        .pin_xclk     = CAM_PIN_XCLK,
        .pin_sccb_sda = CAM_PIN_SIOD,
        .pin_sccb_scl = CAM_PIN_SIOC,
        .pin_d7       = CAM_PIN_D7,
        .pin_d6       = CAM_PIN_D6,
        .pin_d5       = CAM_PIN_D5,
        .pin_d4       = CAM_PIN_D4,
        .pin_d3       = CAM_PIN_D3,
        .pin_d2       = CAM_PIN_D2,
        .pin_d1       = CAM_PIN_D1,
        .pin_d0       = CAM_PIN_D0,
        .pin_vsync    = CAM_PIN_VSYNC,
        .pin_href     = CAM_PIN_HREF,
        .pin_pclk     = CAM_PIN_PCLK,
        .xclk_freq_hz = 20000000,
        .ledc_timer   = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size   = FRAME_SIZE,
        .jpeg_quality = JPEG_QUALITY,
        .fb_count     = 2,
        .fb_location  = CAMERA_FB_IN_DRAM,
        .grab_mode    = CAMERA_GRAB_WHEN_EMPTY,
    };

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_camera_init failed: %s", esp_err_to_name(err));
        return err;
    }
    sensor_t *s = esp_camera_sensor_get();
    if (s) {
        s_sensor = s;
        ESP_LOGI(TAG, "sensor PID=0x%04x VER=0x%02x MIDH=0x%02x MIDL=0x%02x",
                 s->id.PID, s->id.VER, s->id.MIDH, s->id.MIDL);
        s->set_vflip(s, 0);
        s->set_hmirror(s, 0);
        s->set_brightness(s, 0);
        s->set_gainceiling(s, GAINCEILING_2X);
        s->set_ae_level(s, -1);
    }
    return ESP_OK;
}

void app_main(void)
{
    /* 串口未缓冲，每帧一次 write，尽量少切包 */
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    /* 装上 USB-JTAG 驱动：console legacy 路径满 FIFO 会丢字节，驱动是阻塞的 */
    usb_serial_jtag_driver_config_t usb_cfg = {
        .tx_buffer_size = 32768,
        .rx_buffer_size = 1024,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb_cfg));

    ESP_LOGI(TAG, "ESP32-S3 camera stream boot");

    s_tx = heap_caps_malloc(TX_CAP, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!s_tx) {
        ESP_LOGE(TAG, "no internal RAM for tx buffer");
        return;
    }

    if (camera_init() != ESP_OK) {
        ESP_LOGE(TAG, "camera init failed");
        return;
    }

    ESP_LOGI(TAG, "streaming JPEG over USB CDC, magic JPG0");
    esp_log_level_set("*", ESP_LOG_NONE);

    xTaskCreatePinnedToCore(stream_task, "stream", 8192, NULL, 5, NULL, 1);
    xTaskCreatePinnedToCore(rx_task, "rx", 4096, NULL, 3, NULL, 0);
}
