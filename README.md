# J6B 固件刷写工具

一键刷写 J6B MCU 固件：自动打断 uboot → `fastboot udp` → 刷分区 → 重启，失败自动断电重试，全程无人值守（约 35 秒/次）。

## 硬件要求

| 设备 | 连接 | 说明 |
|---|---|---|
| 刷写板 | A 核串口 → PC（当前 N60 EVM 为 **COM30** / SERIAL-C） | 921600，R 核是 COM27 / SERIAL-D |
| 刷写板 | 网线 → PC 同网段 | fastboot 走 UDP |
| KORAD KA3005P | USB → PC（当前为 **COM44**） | 可选；不接则失败时提示人工断电 |
| 刷写板 | 供电接 KA3005P 输出（12V） | 自动断电重试依赖此项 |

软件：Python 3.12+、pyserial、Git Bash（fastboot 在其 PATH 中，当前为 `hobot_tools/platform-tools_r30.0.5-windows`）。

## 使用

```bash
cd Y:/01_Src/tools/flash

# 日常刷写（接电源）
python flash_j6b.py --port COM30 --psu-com COM44

# 不接电源（失败时提示人工断电，回车继续）
python flash_j6b.py --port COM30
```

流程：断电冷启动(3s) → 20ms 周期发空格打断 uboot → `fastboot udp` → 解析板子 IP → `oem interface:mtd` → `flash MCU_a` → `flash MCU_b` → `fastboot reboot`。任一步失败自动断电重来，默认重试 3 次。退出码：0 成功 / 1 失败。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | （必填） | 板子 A 核串口 COM 号 |
| `--psu-com` | 无 | KA3005P 串口 COM 号；不填走人工断电模式 |
| `--config` | `configs/n60_mcu.json` | 刷写配置（分区、镜像、匹配关键字） |
| `--img-dir` | N60 的 `rpu_arm_gcc` 目录 | 镜像相对路径的基目录 |
| `--retries` | 3 | 失败重试次数 |
| `--dry-run` | 关 | 只打断/进 fastboot/解析 IP，不刷写不动电源 |

## 刷其他项目 / 分区

复制 `configs/n60_mcu.json` 改 `commands` 即可，例如刷整包 bin：

```json
{
  "uboot_baudrate": 921600,
  "interrupt_keyword": "Hit any key to stop autoboot",
  "prompt_pattern": "Hobot[$#]",
  "fastboot_ready_pattern": "Listening for fastboot command",
  "commands": [
    {"type": "fastboot", "args": ["oem", "interface:mtd"]},
    {"type": "fastboot", "args": ["flash", "MCU_a", "xxx.img"]},
    {"type": "fastboot", "args": ["flash", "MCU_b", "xxx.img"]}
  ]
}
```

运行时 `--config` 指向新文件，img 相对路径基于 `--img-dir` 解析。

## 换新板校准

新板子/新 uboot 版本先跑 `--dry-run`，对照 `logs/flash_*.log` 里的 `[SER]` 原始打印，确认配置中三个关键字是否匹配：

1. `prompt_pattern` — uboot 提示符（J6B 为 `Hobot$`）
2. `fastboot_ready_pattern` — fastboot 就绪打印（J6B 为 `Listening for fastboot command`）
3. IP 解析 — 从就绪打印中提取 IPv4（取最后一个候选）

## 日志

`logs/flash_YYYYMMDD_HHMMSS.log`，每次运行一个文件：控制台只打关键进度，日志含全部串口原始数据（`[SER]` 前缀）和 fastboot 完整输出。排障先看这里。

## 已知板子特性（2026-09 实测，N60 EVM）

- **bootdelay=0**：看到 `Hit any key to stop autoboot: 0` 再发键来不及，必须从启动打印出现起以 20ms 周期持续发空格（100ms 不够）
- **fastboot 模式下串口 `reset` 无效**，必须用 `fastboot reboot`（UDP 通道）
- 板子 IP（192.168.1.239）来自 uboot-env 静态配置，不依赖 DHCP
- gmac0 无 PHY 会打印 `phy_connect() failed`，实际走 gmac1，属正常

## 模块结构

```
flash_j6b.py       CLI 入口 + 主流程编排（打断/刷写/重试）
uboot_console.py   串口监听、正则匹配、发送命令
korad.py           KA3005P 电源控制（仅开关，电压固定 12V）
fastboot_runner.py 经 Git Bash 调 fastboot
configs/           刷写配置
logs/              运行日志
```
