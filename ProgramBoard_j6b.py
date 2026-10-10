"""J6B 固件刷写主流程。

用法:
  python ProgramBoard_j6b.py --port COM7 [--psu-com COM5] [--config xxx.json]
                      [--img-dir <dir>] [--retries 3] [--dry-run]
"""
import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from korad import KoradError, KoradKA3005P
from uboot_console import UbootConsole, UbootTimeout
from fastboot_runner import FastbootError, run_fastboot, run_fastboot_with_connect_retry

BOOT_PRINT_TIMEOUT = 30       # 等 uboot 启动打印
FASTBOOT_READY_TIMEOUT = 20   # 等 fastboot udp 就绪
FLASH_TIMEOUT = 60            # 单条 fastboot 指令
REBOOT_TIMEOUT = 30           # reset 后等重启打印
POWER_CYCLE_DELAY = 3.0       # 断电放电时间

IPV4_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")


def parse_args():
    here = Path(__file__).parent
    # 工具在 01_Src/tools 共用（J6/TDA4），J6 默认镜像目录按名引用其工作区
    ws = here.parent.parent / "02_Workspace_J6"
    ap = argparse.ArgumentParser(description="J6B 固件刷写工具")
    ap.add_argument("--port", required=True, help="板子 A 核串口 COM 号")
    ap.add_argument("--psu-com", default=None, help="KA3005P 电源串口 COM 号（不填则提示人工断电）")
    ap.add_argument("--config", default=str(here / "configs" / "n60_mcu.json"))
    ap.add_argument("--img-dir", default=str(ws / "src/mcu_app_j6/build/cpj_baic_n60_d310/rpu_arm_gcc"))
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="只监听/解析，不执行 fastboot、不动电源")
    return ap.parse_args()


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def setup_logging(here: Path):
    log_dir = here / "logs"
    log_dir.mkdir(exist_ok=True)
    logfile = log_dir / time.strftime("flash_%Y%m%d_%H%M%S.log")

    logger = logging.getLogger("flash")
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(ch)
    logger.addHandler(fh)

    ser_log = logging.getLogger("flash.serial")
    ser_log.setLevel(logging.DEBUG)
    ser_log.propagate = False
    ser_log.addHandler(fh)

    def log(msg: str):
        logger.info(msg)

    return log, ser_log, logfile


def interrupt_uboot(console: UbootConsole, cfg: dict, log):
    """本板 bootdelay=0：autoboot 检查在打印 'Hit any key' 的同一瞬间完成，
    看到打印再发键来不及。改为从出现启动打印起每 100ms 发一个空格，
    让按键始终垫在串口 RX 里，直到出现 uboot 提示符。"""
    prompt_rx = re.compile(cfg["prompt_pattern"])
    deadline = time.monotonic() + BOOT_PRINT_TIMEOUT
    spam_until = None
    last_key = 0.0
    while time.monotonic() < deadline:
        console.pump()
        text = console.text()
        if prompt_rx.search(text):
            log("打断成功，已进入 uboot 提示符")
            return
        if text and spam_until is None:
            spam_until = time.monotonic() + 10.0
        if spam_until and time.monotonic() < spam_until and \
                time.monotonic() - last_key >= 0.02:
            console.send_raw(b" ")
            last_key = time.monotonic()
    # 超时仍未打断：最后探测一次板子是否已停在提示符
    console.send_line("")
    console.wait_for(cfg["prompt_pattern"], 2)
    log("板子已在 uboot 提示符，无需打断")


def enter_fastboot(console: UbootConsole, cfg: dict, log) -> str:
    """发 fastboot udp，等就绪并解析板子 IP。返回 'udp:<ip>'。"""
    console.clear_buffer()
    console.send_line("fastboot udp")
    console.wait_for(cfg["fastboot_ready_pattern"], FASTBOOT_READY_TIMEOUT)
    ips = IPV4_RE.findall(console.text())
    if not ips:
        raise UbootTimeout("fastboot udp 输出中未解析到 IP")
    ip = ips[-1]
    log(f"板子 IP: {ip}（候选: {ips}）")
    return f"udp:{ip}"


def resolve_args(args_list, img_dir: Path) -> list:
    """flash 指令的 img 参数（第 3 个）若是相对路径则基于 img_dir 解析。"""
    args = list(args_list)
    if args[:1] == ["flash"] and len(args) >= 3:
        p = Path(args[2])
        if not p.is_absolute():
            args[2] = (img_dir / p).as_posix()
    return args


def flash_once(cfg: dict, console: UbootConsole, args, log) -> None:
    interrupt_uboot(console, cfg, log)
    # 可选的 fastboot 前 uboot 命令（如 setenv 静态 IP，解决默认环境 DHCP 超时）
    for cmd in cfg.get("uboot_cmds", []):
        console.clear_buffer()
        console.send_line(cmd)
        console.wait_for(cfg["prompt_pattern"], 5)
        log(f"uboot: {cmd}")
    target = enter_fastboot(console, cfg, log)

    cmds = cfg["commands"]
    if args.dry_run:
        log("[dry-run] 以下指令将执行（本次不执行）:")
        for i, c in enumerate(cmds):
            log(f"  [{i+1}/{len(cmds)}] fastboot {' '.join(resolve_args(c['args'], Path(args.img_dir)))}")
        log("[dry-run] 完成（板子停留在 fastboot udp 状态）")
        return

    for i, c in enumerate(cmds):
        fargs = resolve_args(c["args"], Path(args.img_dir))
        log(f"[{i+1}/{len(cmds)}] fastboot {' '.join(fargs)}")
        if i == 0:
            run_fastboot_with_connect_retry(fargs, target, log, FLASH_TIMEOUT)
        else:
            run_fastboot(fargs, target, log, FLASH_TIMEOUT)

    log("fastboot reboot 重启板子")
    console.clear_buffer()
    run_fastboot(["reboot"], target, log, FLASH_TIMEOUT)
    console.wait_for(cfg["interrupt_keyword"], REBOOT_TIMEOUT)
    log("✅ 刷写完成，板子已重启")


def main() -> int:
    args = parse_args()
    here = Path(__file__).resolve().parent
    log, ser_log, logfile = setup_logging(here)
    cfg = load_config(args.config)
    log(f"配置: {args.config} | 日志: {logfile}")

    psu = None
    if args.psu_com:
        psu = KoradKA3005P(args.psu_com)
        log(f"电源: {args.psu_com} ({psu.identify()})")

    console = UbootConsole(args.port, cfg["uboot_baudrate"],
                           raw_log=lambda d: ser_log.debug("[SER] %s", d.decode(errors="replace")))

    for attempt in range(1, args.retries + 1):
        log(f"=== 第 {attempt}/{args.retries} 次尝试 ===")
        try:
            if args.dry_run:
                log("[dry-run] 跳过电源控制")
            elif psu:
                log(f"断电冷启动（放电 {POWER_CYCLE_DELAY}s）...")
                v = psu.power_cycle(POWER_CYCLE_DELAY)
                log(f"已上电 (VOUT={v}V)")
            elif attempt > 1:
                input("请手动断电、重新上电后按回车继续...")
            console.clear_buffer()
            flash_once(cfg, console, args, log)
            return 0
        except (UbootTimeout, FastbootError, KoradError) as e:
            log(f"❌ 第 {attempt} 次失败: {e}")
        time.sleep(1)

    log(f"❌ {args.retries} 次尝试均失败，详见日志 {logfile}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
