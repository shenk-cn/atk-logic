# atk-logic：ATK-Logic 逻辑分析仪跨平台 Python API / 命令行工具

适用于正点原子（ALIENTEK）DL16 / DL16 Plus 逻辑分析仪（USB `1a86:ffcc`）。
底层通过 libusb（python-libusb1）直接与设备通信。Windows 上可从 PowerShell 后台运行，采集过程没有 GUI。

功能：

- 设备枚举、连接握手、读取设备信息（型号、USB 2.0/3.0、MCU/FPGA/硬件版本、是否 Plus）
- 采集：Buffer（硬件缓存）/ Stream（USB 实时流），支持正常、立即、循环和简单触发。Buffer 支持硬件 RLE
- PWM 输出（2 路，1 Hz–20 MHz，占空比 1–99 %）
- 读取和写出官方上位机工程文件 `.atkdl`
- 导出 CSV（与上位机格式一致的“变化点”行）、VCD（PulseView / sigrok-cli / GTKWave 可读）、无损边沿 JSON（`.atkjson`）、统计摘要 JSON
- 协议解码：用纯 Python 实现的 `sigrokdecode` 兼容层直接运行仓库 `runtime/decoders/` 下的 180 余个 sigrok 解码器（UART、I²C、SPI、CAN、SWD、Modbus、USB PD、WS2812……），支持堆叠（如 I²C → 24xx EEPROM）
- 采集输出 JSON，便于 PowerShell 或其他程序调用

Windows WinUSB 实测通过的设备为 DL16（硬件 2、MCU 1.9、FPGA 2.22、设备要求上位机至少 1.1.0.8）。原版 ATK-Logic 1.1.2.0 可打开 CLI 导出的 `.atkdl`。

## 1. 安装

需要 Python ≥ 3.8。

```bash
cd api
pip install .            # 或开发模式：pip install -e .
# 依赖：libusb1（python-libusb1）、numpy
```

### Windows

- python-libusb1 的 Windows wheel 已内置 `libusb-1.0.dll`，无需另装 libusb。
- 设备需要 WinUSB 驱动。安装过官方 ATK-Logic 上位机的电脑一般已具备；否则可用 [Zadig](https://zadig.akeo.ie/) 为 `VID 1A86 / PID FFCC` 绑定 WinUSB。

在仓库的 `api` 目录安装到独立环境。使用 `atk-logic-cli` 可以避免与官方 `ATK-Logic.exe` 重名。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
$atk = (Resolve-Path .\.venv\Scripts\atk-logic-cli.exe).Path
& $atk limits -c 0-2
```

### Linux

```bash
sudo apt install libusb-1.0-0          # Debian/Ubuntu；其他发行版安装对应 libusb-1.0 包
# 非 root 访问需要 udev 规则（仓库 linux/99-atk-logic.rules）：
sudo cp ../linux/99-atk-logic.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
# 然后重新插拔分析仪
```

### 协议解码器目录

解码器使用仓库中的 `runtime/decoders/`。以开发模式（`pip install -e .`）或直接在仓库内运行时会自动找到；
若把包安装到别处，请设置环境变量 `ATK_DECODERS_DIR=<仓库>/runtime/decoders`，或在命令中加 `--decoders-dir`。

## 2. 命令行

`atk-logic`、`atk-logic-cli` 和 `python -m atk_logic` 调用同一个入口。单次采集向 stdout 输出一个 JSON，循环每轮输出一行 JSON。退出码 `0` 表示成功，`1` 表示文件或其他运行错误，`2` 参数错误，`3` 找不到设备，`4` 无法访问设备（包括被占用），`5` 设备状态异常，`6` 采集失败，`130` 取消。

### JR 三路预设

用 `presets/jr-three-channel.json` 进行 D0（DSC PPM）、D1（JR TX）、D2（JR PPM/SBUS）同步采集。参数为 10 MHz、10 秒、1.6 V、Buffer、立即采集。仪器空闲时，从 `api` 目录执行：

```powershell
& $atk capture --preset .\presets\jr-three-channel.json --events -o .\jr.atkdl
& $atk capture --preset .\presets\jr-three-channel.json --rate 100M --rle --timeout 120s -o .\jr-rle.atkdl
& $atk capture --preset .\presets\jr-three-channel.json --acquisition repeat --repeat-count 3 --interval 2s -o .\jr-series.atkdl
```

- 命令行覆盖预设；`--duration` 和 `--samples` 任选其一，`--no-rle` 和 `--no-trigger` 清除对应预设。`--acquisition immediate` 忽略触发条件。循环按正常模式采集，每轮结束后至少等待指定间隔，按 `jr-series.0001.atkdl` 等名称独立保存。循环发现同名结果会报错。
- `--events` 在仪器确认启动或已回传有效进度/数据时向 stderr 输出 `{"event":"armed","cycle":1}`。正常触发采集收到 `armed` 后仍可等待外部触发。板端脚本可以用这个事件安排动作。最终 JSON 中使用 `capture.meta.complete`、`capture.meta.channel_samples`、`capture.meta.end_reason` 和 `capture.meta.overflow` 判定结果。
- `--stop-file PATH` 监视一个启动时不存在的文件。创建该文件或发送 Ctrl+C 可取消。`--stop-mode save-received` 仅用于 Stream，保存有完整公共时基的已接收部分，结果标记 `complete: false`。普通欠采、超时、断连和溢出均返回失败，采集失败时已有输出文件保留。`.atkdl`、CSV 和 VCD 可重复指定 `-o`，每个文件写完后通过同目录临时文件替换目标。采集默认仅计算轻量摘要，`--stats` 加算边沿统计。
- 100 MHz 三路 10 秒相当于每路 10 亿点，打包波形约 375 MB，另外还需要收包和保存的内存。RLE 的实际采满能力受信号活动和硬件容量影响。以每路真实点数和溢出状态验收。

```bash
atk-logic list                                    # 枚举设备（不打开）
atk-logic info --pretty                           # 连接并读取设备信息
atk-logic limits -c 0-3 --plus                    # 离线查询：该通道数下允许的采样率/深度

# 采集 ch0-3，10 MHz，10 ms，3.3V CMOS 电平（阈值 1.6 V），立即开始，保存 VCD 和 CSV
atk-logic capture -c 0-3 -r 10M -d 10ms --threshold 3.3V -o cap.vcd -o cap.csv

# ch0 上升沿触发，触发前保留 10 %，最多等 5 s
atk-logic capture -c 0,1 -r 100M -n 1M -t 0=rising --trigger-pos 10 --timeout 5s -o trig.atkjson

# Stream 模式长时间采集（采样率 × 通道数 ≤ 320 MHz，且采样率 ≤ 100 MHz）
atk-logic capture -c 0,1 -r 20M -d 2s --mode stream -o long.atkdl

# 采集并直接解码（UART RX 接 ch0，115200 8N1）
atk-logic capture -c 0 -r 10M -d 50ms -D uart -m rx=0 -O baudrate=115200 --ann rx-data

# 自环测试：PWM0 输出 1 MHz 50 % 接到 ch0，同时采集
atk-logic capture -c 0 -r 100M -d 1ms --pwm 0:1M:50 --edges

# PWM：在同一调用内保持 10 s 后关闭
atk-logic pwm 1 -f 2.5M --duty 50 --hold 10s
atk-logic pwm-stop            # 打开会话并关闭两路输出

# 离线：打开上位机保存的 .atkdl，输出统计并导出
atk-logic open ../runtime/test/uart_tx_115200.atkdl -o uart.vcd --pretty
atk-logic edges ../runtime/test/uart_tx_115200.atkdl --start 0.9ms --end 2ms   # 紧凑的电平持续时间文本

# 解码：不给 -D 时使用 .atkdl 里保存的解码配置
atk-logic decode ../runtime/test/eeprom_24c02_i2c.atkdl --rows ops
atk-logic decode ../runtime/test/uart_tx_115200.atkdl -D uart -m tx=0 -O baudrate=115200 -O format=ascii --ann tx-data
atk-logic decode cap.atkjson -D i2c,eeprom24xx -m scl=0,sda=1 --annotations --max-annotations 50
atk-logic decoders                 # 列出全部解码器
atk-logic decoders spi --pretty    # 查看某解码器的通道、选项、注释类别
```

常用参数说明：

| 参数 | 说明 |
|---|---|
| `-c 0,1,4-7` | 通道（0–15） |
| `-r 10M` | 采样率，可选 1/2/4/5/10/20/25/40/50/100/200/250/500 MHz、1 GHz（受模式/型号/通道数限制，见 `limits`） |
| `-d 10ms` / `-n 1M` | 采集时长 / 每通道采样点数 |
| `--threshold` | 阈值电压（-5…5 V）或预设 `5V`/`3.3V`/`3.0V`/`2.5V`/`1.8V`/`1.5V`/`1.2V`（对应上位机 CMOS 预设 2.5/1.6/1.5/1.2/0.9/0.7/0.6 V），默认 1.6 V |
| `-t 0=rising,3=high` | 简单触发支持 `rising`、`falling`、`high`、`low` 和 `edge`（双沿），多路条件同时满足时触发 |
| `--trigger-pos 10` | Buffer 触发前数据占比（1–90 %）。Stream 的触发位置由仪器报告 |
| `-D` / `-m` / `-O` | 解码器（可堆叠，低层在前）/ 通道映射 / 选项（有歧义时写 `decoder.option=value`） |
| `--ann` / `--rows` | 只输出指定注释类别 / 注释行 |

## 3. Python API

```python
from atk_logic import AtkLogic, list_devices, load, read_atkdl, decode

print(list_devices())
cap = load("../runtime/test/uart_tx_115200.atkdl")   # .atkdl（上位机工程）或 .atkjson（本包保存）

with AtkLogic.open() as la:                    # 连接握手与官方上位机相同
    print(la.info.as_dict())
    la.pwm(0, 1e6, 50)                         # 可选：PWM0 输出 1 MHz 方波
    cap = la.capture(channels=[0, 1], samplerate=100e6, duration=1e-3,
                     threshold="3.3V", mode="buffer",
                     trigger={0: "rising"}, trigger_position=10, timeout=5)
# 退出 with 时按上位机流程关闭（唤醒 FPGA → 关 PWM → Exit → 休眠）

if not cap.meta["complete"]:
    raise RuntimeError(cap.meta["end_reason"])

print(cap.summary())                           # 每通道跳变数、占空比、频率、首末边沿……
print(cap.edges_text(max_edges=50))            # 适合直接阅读的电平持续时间文本
bits = cap.channel(0).bits(0, 1000)            # numpy 0/1 数组
edges = cap.channel(0).edges                   # 跳变位置（采样点序号）
cap.save("cap.atkdl")
cap.save("cap.csv")
cap.save("cap.vcd")

res = decode(cap, [{"id": "uart", "channels": {"rx": 1}, "options": {"baudrate": 115200}}])
print(res["rows"])                             # 按注释行汇总的文本
for a in res["annotations"][:10]:              # 带时间戳的注释
    print(a["t0"], a["ann"], a["text"])

old = read_atkdl("../runtime/test/spi_flash_w25q128.atkdl")
print(decode(old, old.meta["decoders"][0])["rows"]["spiflash.commands"])
```

主要异常：`DeviceNotFound`、`DeviceAccessError`（权限/驱动/被占用）、`DeviceStateError`（握手失败、需升级固件）、`CaptureError`；参数不合法时抛 `ValueError`，信息中写明允许范围。异常对象的 `hint` 属性给出处理建议。

## 4. 设计与实现对照

| 本包 | 官方源码 | 内容 |
|---|---|---|
| `protocol.fpga_command` | `pv/usb/usb_control.cpp` `Write()` / `SendToDevice()` | 8 字节 0 + `0x0A` + `[code, len+1, data]` + `0x0B` + CRC32，补齐 2048 字节后做 4 路交织 |
| `protocol.mcu_command` | `SendToMCU()` | 512 字节原始命令（版本查询 0x81、FPGA 唤醒/休眠 0x87…） |
| `protocol.convert_to_pc/device` | `pv/usb/usb_base.cpp` | 每 2048 字节块 4×512 字节交织 |
| `protocol.crc32_atk` | `pv/static/util.cpp` `gCRC32` | 反射 CRC-32 表，初值 0、末尾异或 0xFFFFFFFF |
| `protocol.PacketParser` | `pv/data/analysis.cpp` | `0x0A, order, u16 len, payload, 0x00, 0x0B`，跨传输残包拼接 |
| `protocol.encode_settings/encode_trigger/encode_pwm` | `pv/controller/session_controller.cpp` | 参数、触发、PWM 字节编码 |
| `AtkLogic.connect` | `pv/thread/connect.cpp` | 握手、版本检查、bootloader 检测 |
| `AtkLogic.capture` | `pv/data/session.cpp` `OrderStart`、`pv/thread/thread_work.cpp` `DeviceRecvThread` | 采集流程、order 1–6 数据包处理、RLE 展开、buffer 模式起始偏移裁剪、超容/超带宽标志 |
| `atkdl.read_atkdl` / `write_atkdl` | `ThreadWork::LoadSourceFileThreadPara` / `SaveSourceFileThreadPara` | zip 内 `channel.ini` + 每通道 1 MiB 数据块 + `tog/value` 常量块 |
| `decode.py` | libsigrokdecode `type_decoder.c` / `instance.c` | `wait()` 条件语义（h/l/r/f/e/n/skip、OR/AND、`matched`、`samplenum`） |

解码器引擎不逐点扫描，而是在各通道的跳变列表上跳转，因此长时间空闲的数据几乎不耗时；对跳变极多的数据（例如 5 亿点、1500 万次跳变的 SWD 文件）纯 Python 解码需要数分钟，可用 `--start/--end` 截取窗口或用 `--limit` 限制注释数量提前结束。

## 5. 实机验收与限制

- 实测 D0–D2、1.6 V、Buffer、10 MHz × 10 秒和 100 MHz × 10 秒（RLE），三路均采满。RLE 用例 Windows 进程峰值工作集约 489 MiB。两份 `.atkdl` 均已在原版上位机打开。另通过了 Buffer 和 Stream 上升沿触发、三轮循环、Buffer 取消与 Stream 停止后保存共同样本。
- 官方上位机和 CLI 分时使用同一台仪器。实测上位机占用时 CLI 返回退出码 `4`。`list` 只枚举，`info` 和 `capture` 会打开设备并初始化。握手和默认退出会关闭两路 PWM。使用 `capture --pwm` 可在同一个会话内操作，实际输出和跨进程保持尚未实机验证。
- 高持续带宽的 Stream、长时间等待触发、DL16 Plus 和其他新版设备固件尚未完成本机验证。收到容量或带宽溢出时，CLI 返回采集失败。
- 按厂商手册 §5.2、§5.3.4、§5.4 和 §5.12，DL16 的 RLE 只用于 Buffer。原生滚动、停止后上传 Buffer 和高级触发是 DL32 功能。
- 检测到 bootloader 或不兼容的仪器版本时报错退出。固件升级使用官方工具。
- 采集数据保存在内存中（每通道每 8 个采样点 1 字节），超大深度时请注意内存。

## 6. 测试

```bash
pip install pytest
python -m pytest -q tests
```

测试内容：CRC/帧格式/交织往返、数据包解析（任意切分、垃圾字节）、RLE、参数编码；
用软件模拟设备（`tests/fake_device.py`）端到端测试连接、buffer/stream/RLE 采集（含非整字节的起始偏移裁剪）、触发超时、参数被拒、PWM 与关闭流程；
读取 `runtime/test/*.atkdl` 并用 UART、I²C+EEPROM、SPI+Flash、CAN 解码器校验结果；导出格式往返；CLI 冒烟测试。

## 许可

GPL-3.0-or-later（与 ATK-Logic 及 sigrok 解码器一致）。
