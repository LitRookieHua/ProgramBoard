"""KORAD KA3005P 电源控制：仅开关（电压固定 12V，不做设置）。"""
import re
import time

import serial


class KoradError(Exception):
    pass


class KoradKA3005P:
    def __init__(self, port: str):
        try:
            self._ser = serial.Serial(port, 115200, timeout=1.0)
        except serial.SerialException as e:
            raise KoradError(f"无法打开电源串口 {port}: {e}") from e

    def _cmd(self, cmd: str) -> str:
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\n").encode())
        if cmd.endswith("?"):
            return self._ser.read(64).decode(errors="ignore").strip()
        return ""

    def identify(self) -> str:
        idn = self._cmd("*IDN?")
        if not idn:
            raise KoradError("*IDN? 无响应——请检查 --psu-com 是否填成了板子串口")
        return idn

    def output_on(self):
        self._cmd("OUT1")

    def output_off(self):
        self._cmd("OUT0")

    def measure_voltage(self) -> float:
        raw = self._cmd("VOUT1?")
        m = re.search(r"\d+(?:\.\d+)?", raw)
        if not m:
            raise KoradError(f"VOUT1? 返回异常: {raw!r}")
        return float(m.group())

    def power_cycle(self, delay: float = 3.0) -> float:
        """OUT0 → 放电 → OUT1 → 回读确认输出拉起。返回实测电压。"""
        self.output_off()
        time.sleep(delay)
        self.output_on()
        time.sleep(1.0)
        v = self.measure_voltage()
        if v < 1.0:
            raise KoradError(f"OUT1 后输出未拉起 (VOUT={v}V)")
        return v

    def close(self):
        self._ser.close()


if __name__ == "__main__":
    import sys
    psu = KoradKA3005P(sys.argv[1])
    print("IDN:", psu.identify())
    print("VOUT:", psu.measure_voltage(), "V")
    psu.close()
