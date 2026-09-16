"""J6B OTA 整包刷写流程（板子正常启动到 QNX 后走 SSH，不动 uboot/fastboot）。

用法:
  python ota_j6b.py --port COM30 [--ota <xxx.zip>] [--psu-com COM44]
                    [--host 192.168.1.239] [--retries 3] [--dry-run]

流程（包名不可修改，全程使用原始文件名）:
  1. 上电（可选 PSU 冷启动，不填则假定板子已启动）
  2. 串口使能 SSH：每次上电/重启后 pf 会拦截 SSH，需通过 A 核串口登录
     执行 pfctl -d 并等待 "pf disabled"
  3. SSH 删除 /ota 旧包
  4. SFTP 上传整包（逐块同步写，sync 后做大小+MD5 校验，失败自动重传）
  5. ota_tool -g 记录当前槽位 → ". /etc/profile && cd /ota && ota_tool -p <原名>"
     安装（必须带登录 shell 环境，否则段错误），等 [OTA SUCCESSFULL]
     → 板子自动重启 → 等 60s → 串口使能 SSH → ota_tool -g 确认槽位翻转（A→B / B→A）
"""
import argparse
import hashlib
import logging
import re
import shlex
import sys
import time
from pathlib import Path

import paramiko
import serial

sys.path.insert(0, str(Path(__file__).parent))
from korad import KoradError, KoradKA3005P

BOARD_USER = "root"
BOARD_PASSWORD = "hct@Shanghai0427"
SSH_PORT = 22
ACORE_BAUDRATE = 921600         # A 核串口波特率
POWER_CYCLE_DELAY = 3.0        # 断电放电时间
ENABLE_SSH_TIMEOUT = 180        # 上电/重启后等串口出现 login 提示（QNX 启动）
PFCTL_WAIT = 15                 # pfctl -d 等 "pf disabled"
SSH_READY_TIMEOUT = 300        # 上电/重启后等 SSH 就绪（QNX 完整启动）
OTA_TOOL_TIMEOUT = 1800        # ota_tool -p 全流程
OTA_REBOOT_GRACE = 180         # 见到成功标志后等板子自行重启的宽限
REBOOT_SETTLE = 60             # 成功后等板子启动完（新版系统 SSH 要 1 分钟后才可连）
MD5_TIMEOUT = 600              # 板上 md5sum 554MB 约 1 分钟，留余量
UPLOAD_VERIFY_TRIES = 3        # 上传校验失败重传次数

SLOT_RE = re.compile(r"current slot is:\s*([ABab])")
SUCCESS_RE = re.compile(r"OTA\s*SUCCESSFUL", re.IGNORECASE)


class OtaError(Exception):
    pass


def parse_args():
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description="J6B OTA 整包刷写工具")
    ap.add_argument("--port", required=True,
                    help="板子 A 核串口 COM 号（上电/重启后串口登录执行 pfctl -d 使能 SSH）")
    ap.add_argument("--ota", default=None,
                    help="OTA 整包 zip 路径（缺省取 ota_package/ 下最新 zip）")
    ap.add_argument("--psu-com", default=None,
                    help="KA3005P 电源串口 COM 号（不填则假定板子已上电启动）")
    ap.add_argument("--host", default="192.168.1.239", help="板子 IP")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true",
                    help="只做连接与删旧包检查，不上传不安装")
    return ap.parse_args()


def resolve_ota_package(ota_arg: str) -> Path:
    if ota_arg:
        p = Path(ota_arg)
        if not p.is_file():
            raise OtaError(f"OTA 包不存在: {p}")
        return p
    pkg_dir = Path(__file__).parent / "ota_package"
    zips = sorted(pkg_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
    if not zips:
        raise OtaError(f"{pkg_dir} 下没有 zip，请用 --ota 指定整包路径")
    return zips[-1]


def remote_path_for(pkg: Path) -> str:
    return f"/ota/{pkg.name}"


def setup_logging(here: Path):
    log_dir = here / "logs"
    log_dir.mkdir(exist_ok=True)
    logfile = log_dir / time.strftime("ota_%Y%m%d_%H%M%S.log")

    logger = logging.getLogger("ota")
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(ch)
    logger.addHandler(fh)

    def log(msg: str):
        logger.info(msg)

    return log, logfile


def enable_ssh_via_serial(port: str, log):
    """上电/重启后 QNX 的 pf 会拦截 SSH，必须通过 A 核串口登录执行
    pfctl -d 并等待 "pf disabled"。

    串口登录交互必须用 \r 行结尾（\n 会导致登录流程卡住）。"""
    try:
        ser = serial.Serial(port, ACORE_BAUDRATE, timeout=0.2)
    except serial.SerialException as e:
        raise OtaError(f"无法打开 A 核串口 {port}: {e}") from e
    try:
        deadline = time.monotonic() + ENABLE_SSH_TIMEOUT

        def drain(sec: float) -> bytes:
            """读 sec 秒串口，返回期间收到的原始数据。"""
            out = b""
            end = time.monotonic() + sec
            while time.monotonic() < end:
                n = ser.in_waiting
                if n:
                    out += ser.read(n)
                else:
                    time.sleep(0.05)
            return out

        buf = b""
        logged_in = False
        while time.monotonic() < deadline:
            ser.write(b"\r")
            buf += drain(1.5)
            if b"login:" in buf:
                ser.write(b"root\r")
                buf += drain(1.0)
                ser.write(BOARD_PASSWORD.encode() + b"\r")
                buf += drain(2.0)
            if b"root@hobot" in buf:
                logged_in = True
                break
            buf = b""
            time.sleep(2)
        if not logged_in:
            raise OtaError(f"串口 {port} 登录超时（{ENABLE_SSH_TIMEOUT:.0f}s），"
                           f"最后输出: {buf[-200:]!r}")

        ser.write(b"pfctl -d\r")
        out = drain(PFCTL_WAIT)
        # pf 已关闭时 pfctl -d 会打印 "pf not enabled"，同样代表 SSH 可连
        if b"pf disabled" not in out and b"pf not enabled" not in out:
            raise OtaError(f"pfctl -d 后未见 pf disabled，输出: {out!r}")
        log(f"串口 {port} 已执行 pfctl -d（pf disabled），SSH 已使能")
    finally:
        ser.close()


def connect_ssh(host: str, timeout: float, log):
    """循环尝试连接直到 SSH 就绪（上电/重启后 QNX 启动需要时间）。"""
    deadline = time.monotonic() + timeout
    while True:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(host, port=SSH_PORT, username=BOARD_USER,
                           password=BOARD_PASSWORD, timeout=10,
                           allow_agent=False, look_for_keys=False)
            log(f"SSH 已连接: {BOARD_USER}@{host}")
            return client
        except Exception as e:
            client.close()
            if time.monotonic() >= deadline:
                raise OtaError(f"SSH 连接 {host} 超时（{timeout:.0f}s）: {e}") from e
            time.sleep(3)


def run_cmd(client: paramiko.SSHClient, cmd: str, timeout: float, log,
            stream: bool = False):
    """执行远程命令并收集全部输出。stream=True 时逐行实时打印。"""
    chan = client.get_transport().open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    buf = b""
    output = []
    while True:
        data = chan.recv(4096) if chan.recv_ready() else b""
        if data:
            buf += data
            *lines, buf = buf.split(b"\n")
            for line in lines:
                text = line.decode(errors="replace").rstrip("\r")
                output.append(text)
                if stream:
                    log(text)
        if chan.exit_status_ready() and not chan.recv_ready():
            break
        time.sleep(0.05 if stream else 0.02)
    if chan.recv_ready():
        buf += chan.recv(65536)
    for line in buf.split(b"\n"):
        text = line.decode(errors="replace").rstrip("\r")
        if text:
            output.append(text)
            if stream:
                log(text)
    rc = chan.recv_exit_status()
    chan.close()
    return rc, "\n".join(output)


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upload(sftp: paramiko.SFTPClient, local: Path, log):
    """逐块同步写（非 pipelined）。sftp.put() 默认流水线写，QNX sftp-server
    会静默丢数据还返回成功（实测 554MB 只落盘 245MB），必须逐块等 ACK。"""
    remote = remote_path_for(local)
    size = local.stat().st_size
    t0 = time.monotonic()
    log(f"上传 {local.name}（{size/1e6:.0f} MB）→ {remote}")
    last_t, last_b = t0, 0
    done = 0
    with open(local, "rb") as lf, sftp.open(remote, "wb") as rf:
        while True:
            data = lf.read(32768)
            if not data:
                break
            rf.write(data)
            done += len(data)
            now = time.monotonic()
            if now - last_t >= 5.0:
                speed = (done - last_b) / (now - last_t) / 1e6
                log(f"  上传 {done/1e6:8.0f}/{size/1e6:.0f} MB ({speed:.1f} MB/s)")
                last_t, last_b = now, done
    dt = time.monotonic() - t0
    log(f"上传完成，用时 {dt:.0f}s（平均 {size/1e6/max(dt,1e-9):.1f} MB/s）")


def verify_upload(client: paramiko.SSHClient, sftp: paramiko.SFTPClient,
                  pkg: Path, local_md5: str, log) -> str | None:
    """sync 落盘后校验板上文件大小与 MD5。返回 None 表示一致，否则为错误描述。"""
    remote = remote_path_for(pkg)
    run_cmd(client, "sync", 120, log)
    time.sleep(2)
    st = sftp.stat(remote)
    if st.st_size != pkg.stat().st_size:
        return f"大小不一致: 板上 {st.st_size} / 本地 {pkg.stat().st_size}"
    rc, out = run_cmd(client, f"md5sum {shlex.quote(remote)}", MD5_TIMEOUT, log)
    if rc != 0:
        return f"板上 md5sum 执行失败 exit={rc}"
    remote_md5 = out.split()[0].strip()
    if remote_md5 != local_md5:
        return f"MD5 不一致: 板上 {remote_md5} / 本地 {local_md5}"
    log(f"校验通过: 大小 {pkg.stat().st_size} 字节, MD5 {local_md5}")
    return None


def upload_verified(client: paramiko.SSHClient, pkg: Path, local_md5: str, log):
    sftp = client.open_sftp()
    try:
        for i in range(1, UPLOAD_VERIFY_TRIES + 1):
            upload(sftp, pkg, log)
            err = verify_upload(client, sftp, pkg, local_md5, log)
            if err is None:
                return
            log(f"❌ 第 {i}/{UPLOAD_VERIFY_TRIES} 次上传校验失败: {err}")
            if i < UPLOAD_VERIFY_TRIES:
                log("删除残缺文件后重传...")
                sftp.remove(remote_path_for(pkg))
        raise OtaError(f"上传 {UPLOAD_VERIFY_TRIES} 次均未通过校验")
    finally:
        sftp.close()


def get_slot(client: paramiko.SSHClient, log) -> str:
    """ota_tool -g 解析当前槽位，返回 'A' 或 'B'。"""
    rc, out = run_cmd(client, "ota_tool -g", 60, log)
    if rc != 0:
        raise OtaError(f"ota_tool -g 失败 exit={rc}")
    m = SLOT_RE.search(out)
    if not m:
        raise OtaError(f"未能从 ota_tool -g 输出解析槽位: {out!r}")
    slot = m.group(1).upper()
    log(f"当前槽位: {slot}")
    return slot


def run_install(client: paramiko.SSHClient, pkg: Path, log) -> None:
    """ota_tool -p 原名安装，实时打印输出，等 [OTA SUCCESSFULL] 成功标志。

    标志出现后板子会自动重启：继续排空输出直到通道关闭（重启断连）或进程
    正常退出，二者都算正常收尾。"""
    remote = remote_path_for(pkg)
    log("开始 ota_tool 安装（时间较长，实时输出如下）...")
    chan = client.get_transport().open_session()
    # 必须先加载 /etc/profile（等价登录 shell）：非登录 shell 缺 LD_LIBRARY_PATH
    # 等环境变量时，ota_tool 在启动 ota_process 子进程处直接段错误（exit=139，
    # 实测 PTY / cd /ota / 相对路径均非根因）。PTY + cd /ota 为复刻交互式用法。
    chan.get_pty(term="vt100", width=200, height=50)
    chan.settimeout(OTA_TOOL_TIMEOUT)
    chan.exec_command(f". /etc/profile && cd /ota && ota_tool -p {shlex.quote(pkg.name)}")

    pending = b""
    all_text = ""
    success = False
    deadline = time.monotonic() + OTA_TOOL_TIMEOUT
    while time.monotonic() < deadline:
        if chan.recv_ready():
            data = chan.recv(4096)
            pending += data
            all_text += data.decode(errors="replace")
            *lines, pending = pending.split(b"\n")
            for line in lines:
                log(line.decode(errors="replace").rstrip("\r"))
            if not success and SUCCESS_RE.search(all_text):
                success = True
                log(">>> 检测到 [OTA SUCCESSFULL]，升级完成")
        if success:
            break
        if chan.exit_status_ready() and not chan.recv_ready():
            rc = chan.recv_exit_status()
            raise OtaError(f"ota_tool 异常退出 exit={rc}，未见 [OTA SUCCESSFULL]")
        time.sleep(0.1)
    if not success:
        raise OtaError(f"等待 [OTA SUCCESSFULL] 超时（{OTA_TOOL_TIMEOUT:.0f}s）")

    log("等待板子自动重启...")
    t0 = time.monotonic()
    while time.monotonic() - t0 < OTA_REBOOT_GRACE:
        try:
            if chan.recv_ready():
                pending += chan.recv(4096)
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    log(line.decode(errors="replace").rstrip("\r"))
                continue
            if chan.exit_status_ready() and not chan.recv_ready():
                rc = chan.recv_exit_status()
                log(f"ota_tool 进程退出 exit={rc}")
                return
            if chan.closed or chan.eof_received:
                log("通道已关闭（板子正在重启）")
                return
        except Exception as e:
            log(f"连接已断开（板子已重启）: {type(e).__name__}")
            return
        time.sleep(0.5)
    log(f"等待重启宽限（{OTA_REBOOT_GRACE}s）已到，继续")


def ota_once(args, pkg: Path, local_md5: str, log) -> None:
    client = connect_ssh(args.host, SSH_READY_TIMEOUT, log)
    try:
        rc, _ = run_cmd(client, "cd /ota && rm -f *.zip && sync", 60, log)
        if rc != 0:
            raise OtaError(f"删除旧 OTA 包失败，exit={rc}")
        log("已删除 /ota 下旧 OTA 包")

        if args.dry_run:
            rc, out = run_cmd(client, "df -h /ota", 30, log)
            log(out)
            log("[dry-run] 跳过上传与安装")
            return

        upload_verified(client, pkg, local_md5, log)

        slot_before = get_slot(client, log)
        run_install(client, pkg, log)
    finally:
        client.close()

    log(f"等待 {REBOOT_SETTLE}s 让系统启动完，再使能 SSH 确认槽位翻转...")
    time.sleep(REBOOT_SETTLE)
    enable_ssh_via_serial(args.port, log)
    client = connect_ssh(args.host, SSH_READY_TIMEOUT, log)
    try:
        slot_after = get_slot(client, log)
    finally:
        client.close()
    if slot_after == slot_before:
        raise OtaError(f"槽位未翻转（{slot_before} → {slot_after}），升级未生效")
    log(f"✅ OTA 整包升级成功: 槽位 {slot_before} → {slot_after}")


def main() -> int:
    args = parse_args()
    here = Path(__file__).resolve().parent
    log, logfile = setup_logging(here)
    try:
        pkg = resolve_ota_package(args.ota)
    except OtaError as e:
        log(f"❌ {e}")
        return 1
    log(f"OTA 包: {pkg.name} ({pkg.stat().st_size/1e6:.0f} MB) | 目标: {args.host} | 日志: {logfile}")
    log(f"计算本地 MD5: {pkg.name}")
    local_md5 = file_md5(pkg)
    log(f"本地 MD5: {local_md5}")

    psu = None
    if args.psu_com:
        psu = KoradKA3005P(args.psu_com)
        log(f"电源: {args.psu_com} ({psu.identify()})")

    for attempt in range(1, args.retries + 1):
        log(f"=== 第 {attempt}/{args.retries} 次尝试 ===")
        try:
            if args.dry_run:
                log("[dry-run] 跳过电源控制")
            elif psu:
                log(f"断电冷启动（放电 {POWER_CYCLE_DELAY}s），等待系统启动...")
                v = psu.power_cycle(POWER_CYCLE_DELAY)
                log(f"已上电 (VOUT={v}V)")
            else:
                log("未接电源，假定板子已上电启动")
            enable_ssh_via_serial(args.port, log)
            ota_once(args, pkg, local_md5, log)
            return 0
        except (OtaError, KoradError, paramiko.SSHException, OSError) as e:
            log(f"❌ 第 {attempt} 次失败: {e}")
        time.sleep(1)

    log(f"❌ {args.retries} 次尝试均失败，详见日志 {logfile}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
