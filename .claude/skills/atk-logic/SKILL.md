---
name: atk-logic
description: Capture, measure, decode, or export digital signals with an ALIENTEK DL16 or DL16 Plus and the atk-logic-cli tool. Use for 正点原子 ATK-Logic 逻辑分析仪、抓波形、协议解码, or reading and converting .atkdl files.
---

# ATK-Logic 逻辑分析仪控制

本技能调用工具仓库 `api/` 下的 `atk-logic-cli` 命令或 Python API。工具仓库可以存放在任意项目目录。安装、参数和本机验收范围以仓库中的 `api/README.md` 为准。

## 0. 定位命令

从 PATH 调用 `atk-logic-cli`，或在 Windows 用现有项目虚拟环境中的可执行文件。已知仓库路径时，可从任意目录调用：

```powershell
& "$repo\api\.venv\Scripts\atk-logic-cli.exe" --version
```

`$repo` 指工具克隆目录。工具尚未安装时，按 `api/README.md` 安装到项目环境。旧入口 `atk-logic` 及同一环境中的 `python -m atk_logic` 接受相同参数。单次采集输出一个 JSON，循环每轮输出一行 JSON。只处理已有波形时，直接调用 `open`、`edges` 或 `decode`。需要实机时先用 `list` 枚举设备。

## 1. 安全规则（必须遵守）

1. **PWM 会在引脚上输出信号**：开启前向用户说明会输出到哪个 PWM 口、频率和占空比，确认没有接到
   不能被驱动的线路后再执行；用完 `atk-logic pwm-stop`。
2. **绝不尝试固件升级**；遇到 bootloader / 固件版本错误，让用户用官方 ATK-Logic 软件处理。
3. GUI 与 CLI 分时使用同一台仪器。Windows 原版占用时，CLI 可能返回 `LIBUSB_ERROR_ACCESS` 和退出码 `4`。先核对仪器是否被占用。`list` 只枚举，`info` 会打开并初始化设备。
4. 阈值电压要与被测电平匹配（默认 1.6 V 对应 3.3 V CMOS；1.8 V 系统用 `--threshold 1.8V`，
   5 V 系统用 `--threshold 5V`）。不确定被测电平时先问用户，不要猜。
5. 长时间或大深度采集（> 1e8 样本）前先告诉用户预计耗时与数据量。

## 2. 常用流程

### 2.1 设备信息与参数范围
```bash
atk-logic info --pretty                 # 型号、USB2/3、固件版本、是否 Plus
atk-logic limits -c 0-3 [--plus]        # 离线查询该通道组合允许的采样率/深度
```
采样率只能取：1/2/4/5/10/20/25/40/50/100/200/250/500 MHz、1 GHz。
- buffer 模式（默认，存入设备内存后上传，可触发）：标准版 ≤250 MHz，Plus ≤500 MHz，
  Plus 且通道 ≤8 时可 1 GHz；总量 样本数×通道数 ≤ 1e9（Plus 3.5e9），开 `--rle` 再×10（仅适合变化少的信号）。
- Stream 模式（`--mode stream`，USB 连续传输）：≤100 MHz 且采样率×通道数 ≤ 320 MHz。支持简单触发，触发位置由仪器报告。硬件 RLE 用于 Buffer 模式。
- 采样率至少取被测信号最高频率的 4~10 倍；需要精确测时序时取更高。

### 2.2 采集
```bash
# 立即采集 4 个通道 10 ms，保存为 VCD（PulseView/GTKWave 可打开）和 atkjson（供后续解码）
atk-logic capture -c 0-3 -r 10M -d 10ms -o cap.vcd -o cap.atkjson

# 通道 0 上升沿触发，10% 预触发，5 s 内没有触发就放弃
atk-logic capture -c 0,1 -r 100M -n 1M -t 0=rising --trigger-pos 10 --timeout 5s -o trig.atkjson

# 流式采集并保存为原版可打开的 .atkdl
atk-logic-cli capture -c 0,1 -r 20M -d 2s --mode stream -o long.atkdl

# 使用 JR 三路预设，等待仪器确认开始后再触发板端动作
atk-logic-cli capture --preset "<仓库>/api/presets/jr-three-channel.json" --events -o jr.atkdl

# 采集同时直接解码
atk-logic capture -c 0 -r 10M -d 50ms -D uart -m rx=0 -O baudrate=115200 --ann rx-data
```
默认摘要提供各路实际采样点数，`--stats` 额外计算跳变、占空比、频率和最短脉宽。报告完整采集前检查 `capture.meta.complete`、`channel_samples`、`end_reason` 和 `overflow`。`--events` 的 `armed` 在 stderr，表示仪器已确认开始，正常触发模式仍可能在等外部触发。使用 `--timeout` 可限制等待和采集的总时长。

循环用 `--acquisition repeat --repeat-count` 和 `--interval`，每轮独立归档。任务可创建 `--stop-file` 指定的文件来停止采集。Buffer 取消时丢弃数据，Stream 可用 `--stop-mode save-received` 保存已收到的共同样本，结果标记 `complete=false`。

### 2.3 查看波形细节
```bash
atk-logic edges cap.atkjson --start 0.9ms --end 2ms --channels 0   # 紧凑的电平/持续时间文本
atk-logic open FILE.atkdl --pretty                                 # 官方软件保存的工程文件 → 摘要
atk-logic open FILE.atkdl -o out.vcd -o out.csv                    # 格式转换
```
大文件先用摘要和 `--start/--end` 缩小窗口，不要一次性输出全部边沿。

### 2.4 协议解码（sigrok 解码器，180+ 种）
```bash
atk-logic decoders                       # 列出解码器 id
atk-logic decoders spi --pretty          # 某个解码器的通道名、选项、注释类别
atk-logic decode FILE.atkdl              # 使用 .atkdl 中保存的解码配置
atk-logic decode cap.atkjson -D uart -m tx=0 -O baudrate=115200 -O format=ascii --ann tx-data
atk-logic decode cap.atkjson -D i2c,eeprom24xx -m scl=0,sda=1 --rows ops
atk-logic decode cap.atkjson -D spi,spiflash -m clk=0,mosi=1,miso=2,cs=3
atk-logic decode --capture -c 0,1 -r 10M -d 20ms -D i2c -m scl=0,sda=1 -o cap.atkjson
```
- `-D` 用逗号写解码器栈（底层在前）；`-m` 把解码器通道名映射到分析仪通道号；`-O` 设置选项，
  栈中有重名选项时写成 `uart.parity=even`。通道名/选项名不确定时先运行 `atk-logic decoders ID`。
- `--rows`/`--ann` 过滤输出；`--annotations --max-annotations 50` 输出带时间戳的注释；
  默认最多 100000 条注释（`--limit`），大文件请配合 `--start/--end`。
- 波特率未知时，可从 `capture --stats` 的 `shortest_pulse_hz` 估算，再尝试相近的标准波特率解码。

### 2.5 PWM 输出（先征得用户同意）
```bash
atk-logic pwm 1 -f 2.5M --hold 10s     # 输出 10 s 后自动停止
atk-logic pwm-stop                     # 关闭两路输出
# 自检：PWM0 用杜邦线接到通道 0，边输出边采集
atk-logic capture -c 0 -r 100M -d 1ms --pwm 0:1M:50
```
范围 1 Hz 至 20 MHz，占空比 1% 至 99%。实际频率以 `actual_hz` 为准。初始化握手或默认退出会关闭两路 PWM。采集期间需要维持 PWM 时，使用同一条 `capture --pwm` 命令或同一个 Python 会话。实际输出与跨进程保持尚未在本机验收。

## 3. Python API（复杂分析时使用）

```python
from atk_logic import AtkLogic, load, decode

with AtkLogic.open() as la:                      # 多台设备时 AtkLogic.open(index=1)
    print(la.info)
    cap = la.capture(channels=[0, 1], samplerate=10e6, duration=0.01,
                     threshold="3.3V", trigger={0: "falling"}, trigger_position=10, timeout=5)

# 直接从仪器采集时，检查 cap.meta["complete"] 后再按完整波形归档。
cap = load("FILE.atkdl")                         # 也可读取 .atkjson
print(cap.summary())                             # 与 CLI 相同的统计
print(cap.edges_text(channels=[0], start=0, end=int(0.002 * cap.samplerate)))  # 起止为样本序号
cap.save("x.atkdl")                              # 也支持 .vcd / .csv / .atkjson / .json
res = decode(cap, [{"id": "uart", "channels": {"rx": 0}, "options": {"baudrate": 115200}}])
```
需要逐样本处理时用 `ch = cap.channel(0)`：`ch.bits(start, end)` 返回 numpy 0/1 数组，
`ch.edges` 为跳变位置（样本序号），`ch.value_at(i)` 取某一样本；详见 `<仓库>/api/README.md`。

## 4. 错误处理

失败时输出 `{"error": ..., "type": ..., "hint": ...}`，按 `hint` 处理并转告用户。退出码：
2 参数错误，3 未找到设备，4 权限/驱动/被占用，5 设备状态异常（固件/bootloader），6 采集失败，130 取消。

| 现象 | 处理 |
| --- | --- |
| 未找到设备 (3) | 检查连线；Linux `lsusb \| grep 1a86:ffcc`，Windows 设备管理器 |
| Linux 权限不足 (4) | `sudo <仓库>/linux/install-udev-rules.sh` 后重新插拔 |
| Windows 驱动 (4) | 安装官方 ATK-Logic 软件（自带 WinUSB 驱动），或用 Zadig 给 1A86:FFCC 装 WinUSB |
| busy / access (4) | 核对设备持有者。原版软件释放 USB 后再运行 CLI |
| 通讯超时 / 无触发 (6) | 检查触发条件与阈值、加 `--timeout`；仍失败则重新插拔 |
| 固件 / bootloader (5) | 让用户用官方软件升级固件，本工具不刷固件 |

## 5. 汇报结果

向用户汇报时给出：采集参数（通道、采样率、时长、阈值、触发）、关键测量值（频率、占空比、脉宽，
注明分辨率 = 1/采样率）、解码出的数据（较长时给摘要和前若干条）、`meta.warnings` 中的告警，
以及保存的文件路径。测量结论受采样率限制时要说明误差范围。
