"""串口控制台：监听打印、正则匹配、发送命令/打断字符。"""
import re
import time

import serial


class UbootTimeout(Exception):
    pass


class UbootConsole:
    def __init__(self, port: str, baudrate: int = 921600, raw_log=None):
        try:
            self._ser = serial.Serial(port, baudrate, timeout=0.5)
        except serial.SerialException as e:
            raise UbootTimeout(f"无法打开板子串口 {port}: {e}") from e
        self._raw_log = raw_log  # callable(bytes)，可为 None
        self._buf = b""

    def pump(self):
        """非阻塞读一次串口并入缓冲（无数据立即返回）。"""
        n = self._ser.in_waiting
        if n:
            data = self._ser.read(n)
            if self._raw_log:
                self._raw_log(data)
            self._buf += data

    def text(self) -> str:
        return self._buf.decode(errors="replace")

    def clear_buffer(self):
        self._buf = b""

    def wait_for(self, pattern: str, timeout: float):
        """循环读串口直到正则匹配累计缓冲。返回 re.Match，超时抛 UbootTimeout。"""
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = rx.search(self.text())
            if m:
                return m
            self.pump()
        raise UbootTimeout(f"等待 /{pattern}/ 超时 ({timeout}s)")

    def send_raw(self, data: bytes):
        self._ser.write(data)

    def send_line(self, cmd: str):
        self._ser.write((cmd + "\n").encode())

    def close(self):
        self._ser.close()
