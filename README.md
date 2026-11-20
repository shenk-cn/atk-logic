![atk-logo](./img/logo.png)

## 后台采集工具与技能

本仓库包含独立运行的 ATK-Logic 命令行工具和 Python API。Windows 推荐从项目自己的 Python 虚拟环境运行 `atk-logic-cli.exe`。它可在后台配置和采集 DL16，并导出 `.atkdl`、变化点 CSV 和 VCD。Windows 上已实测三路 10 MHz × 10 秒和三路 100 MHz × 10 秒 RLE。 [安装、命令及验收范围](api/README.md)

- `api/` 包含命令行、Python API、测试和 [JR 三路预设](api/presets/jr-three-channel.json)。
- 在 Codex 打开本仓库，可通过 `$atk-logic` 调用项目技能，例如 `$atk-logic 使用 JR 三路预设采集并导出 .atkdl`。 [Codex 项目技能](.agents/skills/atk-logic/SKILL.md)
- 可将技能目录放到 `~/.agents/skills/atk-logic/` 供其他项目使用，Windows 路径为 `%USERPROFILE%\.agents\skills\atk-logic\`。跨项目调用时让 `atk-logic-cli` 可从 PATH 找到，或指定工具检出目录。 [Codex 技能文档](https://developers.openai.com/codex/skills#where-codex-loads-local-skills)
- [`.claude/skills/atk-logic/`](.claude/skills/atk-logic/SKILL.md) 是调用这个工具的 Claude Code 技能。从仓库中使用 Claude Code 时可直接发现。需要在其他项目使用时，只需将 `atk-logic` 技能目录复制到用户的 `.claude/skills/`，工具仍从原项目运行。

## 项目来源

本仓库继承正点原子 ATK-Logic 的上位机源码，并在 huatanshaonian 社区分支提供的 Python API 和 CLI 上继续开发。原有提交历史和 GPL-3.0-or-later 许可随仓库保留。 [原厂源码](https://github.com/alientek-openedv/atk-logic) · [社区分支](https://github.com/huatanshaonian/atk-logic) · [许可证](COPYING)


软件简介(ATK-Logic)
-----------------------

ATK-Logic软件是正点原子团队(ALIENTEK)专为逻辑分析仪设计的功能强大的软件工具，旨在帮助用户进行逻辑信号的捕获、分析和调试。无论您是电子工程师、嵌入式系统开发人员还是电路设计师，该软件都能提供您所需的强大功能和便捷操作。主要特点和功能如下：
- **捕获和显示信号**：该软件能够连接逻辑分析仪，实时捕获并显示逻辑信号的波形。您可以轻松选择感兴趣的信号通道，并以直观的方式查看和分析信号的状态和时序。
- **高级信号分析**：除了基本的波形显示功能，该软件还提供了丰富的高级信号分析工具。您可以进行信号的时序分析、参数测量、协议解码等，帮助您深入理解和解决数字信号相关的问题。
- **强大的调试功能**：逻辑分析仪上位机软件提供了一系列强大的调试功能，包括信号的触发设置、缓冲区深度控制等。这些功能可以帮助您精确捕获和分析复杂的逻辑信号，并加快故障排查的速度。
- **数据导出和工程参数保存**：该软件支持将捕获的信号数据导出为常见的CSV文件格式，方便与其他工具进行数据分析。此外，您还可以保存工程配置，记录分析过程和结果。

ATK-Logic软件的用户界面简洁直观，操作简单易学。它为用户提供了一个高效的工作环境，使得逻辑信号的捕获、分析和调试变得更加便捷和准确。无论您是初学者还是经验丰富的专业人士，该软件都能满足您的需求。如果您有更好的想法或者建议，欢迎联系我们。


资料下载
-----------------------

- 下载地址: [http://www.openedv.com/docs/tool/luojifenxiyi/DL16.html](http://www.openedv.com/docs/tool/luojifenxiyi/DL16.html)


源码环境
-----------------------

```
特殊说明：我们不对源码提供任何技术支持服务
```

- atk_libsigrokdecode : https://github.com/alientek-openedv/atk_libsigrokdecode.git
- libusb-1.0 
- qBreakpad v1.0


Linux 版
-----------------------

源码可在 Linux 上编译运行（Ubuntu 24.04 / Qt 5.15 下验证），一键构建：

```bash
linux/build.sh --deps      # 安装依赖并构建，产物在 build/linux-release/
sudo linux/install.sh      # 可选：安装到 /opt/ATK-Logic，并安装 USB 权限规则
```

详见 [linux/README.md](linux/README.md)。


版权许可
-----------------------

GNU General Public License(GPL), version 3 or later.


联系我们
-----------------------

#### 购买地址：

- 天猫：https://zhengdianyuanzi.tmall.com
- 淘宝：https://openedv.taobao.com 

#### 公司信息

- 公司网址	：www.alientek.com
- 技术论坛	：http://www.openedv.com/forum.php 
- 在线教学	：www.yuanzige.com 
- B站视频	：https://space.bilibili.com/394620890 
- 传真：020-36773971
- 电话：020-38271790

