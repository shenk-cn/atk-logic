---
name: atk-logic
description: 用 atk-logic-cli 在后台配置、采集正点原子 ATK-Logic DL16 系列的数字信号，或离线查看、解码、转换 .atkdl 波形。适用于逻辑分析仪、抓波形、协议解码和采集导出任务。
---

# ATK-Logic 逻辑分析仪

使用仓库 `api/` 的 `atk-logic-cli`。从项目根目录运行时，Windows 可调用 `.\api\.venv\Scripts\atk-logic-cli.exe`，Linux 可调用 PATH 中的 `atk-logic-cli`。缺少工具时，按 [安装、命令和验收范围](../../../api/README.md) 配置项目虚拟环境。在其他项目使用这个技能时，从 PATH 或实际工具检出目录定位 CLI 和 `api/README.md`。

已有波形时，使用 `open` 查看或转换、`edges` 查看指定时间段、`decode` 解码。这些命令处理文件时不占用仪器。

## 实机采集

1. 用 `list` 枚举仪器并确认所选设备。`info` 会打开并初始化设备，可能停止现有的 PWM 输出。CLI 与原厂上位机分时使用 USB。访问失败时先核对设备是否被占用，再决定是否需要释放或检查驱动。
2. 根据被测信号和任务设置通道、阈值、采样率、时长或样本数、模式及触发。做 JR 三路测试时可从仓库根目录用 `capture --preset api/presets/jr-three-channel.json --events -o jr.atkdl`，按本次需求覆盖预设。DL16 的 RLE 用于 Buffer。原生滚动、Buffer 停止后上传和高级触发适用于 DL32。PWM 会驱动引脚，先确认线路和输出参数。
3. 联动被测设备时，用 `--events` 等待 stderr 中的 `armed`。触发式采集此时可能仍在等待触发。需要逐轮采集时使用 `--acquisition repeat`、`--repeat-count` 和 `--interval`。取消可用 Ctrl+C 或创建 `--stop-file` 指定的文件。只有明确需要保存 Stream 已接收部分时，才使用 `--stop-mode save-received`。
4. 报告结果时核对退出码、`capture.meta.complete`、`channel_samples`、`end_reason` 和 `overflow`，并给出实际采样参数和导出路径。标明有意停止得到的部分数据。需要脉宽或频率统计时用 `--stats`，大文件优先读取相关时间窗。
