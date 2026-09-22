#!/usr/bin/env python3
"""PyQt6 hardware acceptance panel for the OpenLibUptech compatibility library.

The GUI deliberately loads ``libuptech.so`` through ctypes instead of importing
the legacy ``uptech.py`` wrapper.  Importing that wrapper would start the fan at
full duty in ``UpTech.__init__``.  This panel performs no actuator write until
the corresponding safety control is explicitly unlocked.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import json
import os
import platform
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from PyQt6.QtCore import QTimer, Qt
    from PyQt6.QtGui import QColor, QCloseEvent, QFont
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QSplitter,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - user-facing dependency error
    raise SystemExit(
        "PyQt6 is required. On Raspberry Pi OS/Debian run: "
        "sudo apt install python3-pyqt6"
    ) from exc


CORE_SYMBOLS = (
    "adc_io_open",
    "adc_io_close",
    "ADC_GetAll",
    "adc_io_InputGetAll",
    "adc_io_ModeGetAll",
    "adc_io_ModeSetAll",
    "adc_io_ModeSet",
    "adc_io_Set",
    "adc_io_SetAll",
    "adc_led_set",
    "cds_servo_open",
    "cds_servo_close",
    "cds_servo_SendFrame",
    "cds_servo_ReadReg",
    "cds_servo_SetMode",
    "cds_servo_SetAngle",
    "cds_servo_SetSpeed",
    "cds_servo_GetPos",
    "mpu6500_open",
    "mpu6500_dmp_init",
    "mpu6500_Get_Accel",
    "mpu6500_Get_Gyro",
    "mpu6500_Get_Attitude",
    "mpu_get_gyro_fsr",
    "mpu_get_accel_fsr",
    "mpu_set_gyro_fsr",
    "mpu_set_accel_fsr",
)

LCD_SYMBOLS = (
    "lcd_open",
    "lcd_close",
    "LCD_Refresh",
    "LCD_SetFont",
    "UG_SetForecolor",
    "UG_SetBackcolor",
    "UG_FillScreen",
    "UG_PutString",
    "UG_FillFrame",
    "UG_FillRoundFrame",
    "UG_FillCircle",
    "UG_DrawMesh",
    "UG_DrawFrame",
    "UG_DrawRoundFrame",
    "UG_DrawPixel",
    "UG_DrawCircle",
    "UG_DrawArc",
    "UG_DrawLine",
)

STATUS_COLORS = {
    "未测试": "#6b7280",
    "通过": "#15803d",
    "失败": "#b91c1c",
    "不支持": "#a16207",
    "已禁用": "#6b7280",
}


@dataclass
class CallResult:
    name: str
    result: int
    err: int

    @property
    def error_text(self) -> str:
        if not self.err:
            return "OK"
        return f"{errno.errorcode.get(self.err, 'ERR')} ({os.strerror(self.err)})"


class UptechLibrary:
    """Small, explicit ctypes binding for the reconstructed ABI."""

    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())
        self.cdll = ctypes.CDLL(self.path, use_errno=True)
        missing = [name for name in CORE_SYMBOLS + LCD_SYMBOLS if not hasattr(self.cdll, name)]
        if missing:
            raise RuntimeError("missing ABI symbols: " + ", ".join(missing))
        self._bind()

    def _set(self, name: str, args: list[Any] | None = None) -> None:
        function = getattr(self.cdll, name)
        function.restype = ctypes.c_int
        if args is not None:
            function.argtypes = args

    def _bind(self) -> None:
        u8p = ctypes.POINTER(ctypes.c_uint8)
        u16p = ctypes.POINTER(ctypes.c_uint16)
        fp = ctypes.POINTER(ctypes.c_float)
        i8p = ctypes.POINTER(ctypes.c_int8)

        for name in ("adc_io_open", "adc_io_close", "adc_io_InputGetAll"):
            self._set(name, [])
        self._set("ADC_GetAll", [u16p])
        self._set("adc_io_ModeGetAll", [u8p])
        self._set("adc_io_ModeSetAll", [ctypes.c_int])
        self._set("adc_io_ModeSet", [ctypes.c_uint, ctypes.c_int])
        self._set("adc_io_Set", [ctypes.c_uint, ctypes.c_int])
        self._set("adc_io_SetAll", [ctypes.c_uint])
        self._set("adc_led_set", [ctypes.c_int, ctypes.c_int])

        for name in ("cds_servo_open", "cds_servo_close"):
            self._set(name, [])
        self._set(
            "cds_servo_SendFrame",
            [ctypes.c_int, ctypes.c_int, ctypes.c_int, u8p],
        )
        self._set(
            "cds_servo_ReadReg",
            [ctypes.c_int, ctypes.c_int, u8p, ctypes.c_int],
        )
        self._set("cds_servo_SetMode", [ctypes.c_int, ctypes.c_int])
        self._set("cds_servo_SetAngle", [ctypes.c_int, ctypes.c_int, ctypes.c_int])
        self._set("cds_servo_SetSpeed", [ctypes.c_int, ctypes.c_int])
        self._set("cds_servo_GetPos", [ctypes.c_int])

        for name in ("mpu6500_open", "mpu6500_dmp_init"):
            self._set(name, [])
        for name in ("mpu6500_Get_Accel", "mpu6500_Get_Gyro", "mpu6500_Get_Attitude"):
            self._set(name, [fp])
        self._set("mpu_get_gyro_fsr", [u16p])
        self._set("mpu_get_accel_fsr", [i8p])
        self._set("mpu_set_gyro_fsr", [ctypes.c_uint])
        self._set("mpu_set_accel_fsr", [ctypes.c_int])

    def call(self, name: str, *args: Any) -> CallResult:
        ctypes.set_errno(0)
        result = int(getattr(self.cdll, name)(*args))
        return CallResult(name=name, result=result, err=ctypes.get_errno())


def inspect_elf(path: str) -> str:
    try:
        data = Path(path).read_bytes()[:20]
    except OSError as exc:
        return f"无法读取: {exc}"
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return "不是 ELF 文件"
    bits = {1: "ELF32", 2: "ELF64"}.get(data[4], "ELF?")
    byte_order = "little" if data[5] == 1 else "big"
    machine = int.from_bytes(data[18:20], byte_order)
    machine_name = {40: "ARM", 183: "AArch64"}.get(machine, f"machine={machine}")
    return f"{bits} {machine_name}"


def initial_library_path(argument: str | None) -> str:
    candidates = [
        argument,
        os.environ.get("UPTECH_LIBRARY"),
        str(Path.cwd() / "libuptech.so"),
        str(Path.home() / "libuptech64" / "libuptech.so"),
        "/usr/local/lib/libuptech.so",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return argument or "./libuptech.so"


class AcceptanceWindow(QMainWindow):
    def __init__(self, library_path: str):
        super().__init__()
        self.setWindowTitle("OpenLibUptech 硬件验收")
        self.resize(1120, 820)

        self.library: UptechLibrary | None = None
        self.results: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.adc_opened = False
        self.cds_opened = False
        self.motor_pulse_active = False
        self.fan_pi: Any = None
        self.fan_output_owned = False

        self.mpu_timer = QTimer(self)
        self.mpu_timer.setInterval(200)
        self.mpu_timer.timeout.connect(self.read_mpu)
        self.adc_timer = QTimer(self)
        self.adc_timer.setInterval(200)
        self.adc_timer.timeout.connect(self.read_adc)
        self.motor_stop_timer = QTimer(self)
        self.motor_stop_timer.setSingleShot(True)
        self.motor_stop_timer.timeout.connect(self.emergency_stop)

        self._build_ui(library_path)
        self.refresh_environment()
        if Path(library_path).expanduser().exists():
            self.load_library()

    def _build_ui(self, library_path: str) -> None:
        root = QWidget(self)
        root_layout = QVBoxLayout(root)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview_tab(library_path), "环境与 ABI")
        self.tabs.addTab(self._build_mpu_tab(), "MPU6500")
        self.tabs.addTab(self._build_adc_tab(), "ADC / IO")
        self.tabs.addTab(self._build_fan_tab(), "风扇")
        self.tabs.addTab(self._build_motor_tab(), "有刷底盘电机")

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(1500)
        fixed_font = QFont("monospace")
        fixed_font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.log_box.setFont(fixed_font)
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.log_box)
        splitter.setSizes([620, 180])
        root_layout.addWidget(splitter)
        self.setCentralWidget(root)
        self.statusBar().showMessage("默认只读；所有执行器写操作均已锁定")

    def _build_overview_tab(self, library_path: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        path_group = QGroupBox("兼容库")
        path_layout = QHBoxLayout(path_group)
        self.library_path = QLineEdit(library_path)
        browse = QPushButton("选择…")
        browse.clicked.connect(self.browse_library)
        load = QPushButton("加载并检查 ABI")
        load.clicked.connect(self.load_library)
        path_layout.addWidget(self.library_path, 1)
        path_layout.addWidget(browse)
        path_layout.addWidget(load)
        layout.addWidget(path_group)

        self.environment_table = QTableWidget(7, 3)
        self.environment_table.setHorizontalHeaderLabels(["检查项", "状态", "详情"])
        self.environment_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.environment_table.verticalHeader().setVisible(False)
        self.environment_rows = {
            "library": 0,
            "abi": 1,
            "i2c": 2,
            "spi": 3,
            "uart": 4,
            "gpio": 5,
            "pigpiod": 6,
        }
        labels = {
            "library": "libuptech.so",
            "abi": "ABI 符号",
            "i2c": "MPU I²C",
            "spi": "ADC SPI",
            "uart": "底盘 UART",
            "gpio": "方向 GPIO chip",
            "pigpiod": "pigpiod",
        }
        for key, row in self.environment_rows.items():
            self.environment_table.setItem(row, 0, QTableWidgetItem(labels[key]))
            self._set_environment(key, "未测试", "")
        layout.addWidget(self.environment_table)

        buttons = QHBoxLayout()
        refresh = QPushButton("刷新环境")
        refresh.clicked.connect(self.refresh_environment)
        safe_suite = QPushButton("运行只读验收")
        safe_suite.clicked.connect(self.run_read_only_suite)
        export = QPushButton("导出 JSON 报告")
        export.clicked.connect(self.export_report)
        buttons.addWidget(refresh)
        buttons.addWidget(safe_suite)
        buttons.addStretch(1)
        buttons.addWidget(export)
        layout.addLayout(buttons)

        note = QLabel(
            "只读验收会检测 MPU、读取 ADC/IO、读取 7/8 号底盘控制器位置并连接 pigpiod；"
            "不会启动风扇，也不会向电机发送非零速度。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _build_mpu_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        detect = QPushButton("检测 WHO_AM_I（只读）")
        detect.clicked.connect(self.detect_mpu)
        initialise = QPushButton("初始化量程（写寄存器）")
        initialise.clicked.connect(self.initialise_mpu)
        once = QPushButton("读取一次")
        once.clicked.connect(self.read_mpu)
        self.mpu_live = QCheckBox("5 Hz 实时刷新")
        self.mpu_live.toggled.connect(self.toggle_mpu_live)
        controls.addWidget(detect)
        controls.addWidget(initialise)
        controls.addWidget(once)
        controls.addWidget(self.mpu_live)
        controls.addStretch(1)
        layout.addLayout(controls)

        values = QGroupBox("原始传感器")
        form = QFormLayout(values)
        self.mpu_identity = QLabel("未测试")
        self.accel_value = QLabel("—")
        self.gyro_value = QLabel("—")
        self.fsr_value = QLabel("—")
        form.addRow("芯片识别", self.mpu_identity)
        form.addRow("加速度 (g)", self.accel_value)
        form.addRow("角速度 (°/s)", self.gyro_value)
        form.addRow("配置量程", self.fsr_value)
        layout.addWidget(values)

        unsupported = QLabel(
            "姿态角 / DMP：当前兼容库明确返回 ENOTSUP；零值不能作为有效姿态参与验收。"
        )
        unsupported.setStyleSheet("color: #a16207; font-weight: 600;")
        unsupported.setWordWrap(True)
        layout.addWidget(unsupported)
        layout.addStretch(1)
        return page

    def _build_adc_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        open_button = QPushButton("打开 SPI")
        open_button.clicked.connect(self.open_adc)
        close_button = QPushButton("关闭 SPI")
        close_button.clicked.connect(self.close_adc)
        read_button = QPushButton("读取一次")
        read_button.clicked.connect(self.read_adc)
        self.adc_live = QCheckBox("5 Hz 实时刷新")
        self.adc_live.toggled.connect(self.toggle_adc_live)
        controls.addWidget(open_button)
        controls.addWidget(close_button)
        controls.addWidget(read_button)
        controls.addWidget(self.adc_live)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.adc_table = QTableWidget(10, 2)
        self.adc_table.setHorizontalHeaderLabels(["通道", "原始值"])
        self.adc_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.adc_table.verticalHeader().setVisible(False)
        for index in range(10):
            self.adc_table.setItem(index, 0, QTableWidgetItem(f"ADC{index}"))
            self.adc_table.setItem(index, 1, QTableWidgetItem("—"))
        layout.addWidget(self.adc_table)

        self.io_value = QLabel("输入掩码：—    模式掩码：—")
        layout.addWidget(self.io_value)

        write_group = QGroupBox("IO / LED 写操作（默认锁定）")
        write_layout = QGridLayout(write_group)
        self.adc_write_unlock = QCheckBox("我确认已核对接线，允许写 ADC 扩展板输出")
        self.adc_write_unlock.toggled.connect(self.toggle_adc_write_unlock)
        self.io_channel = QSpinBox()
        self.io_channel.setRange(0, 7)
        self.io_mode = QComboBox()
        self.io_mode.addItems(["原始模式位 0", "原始模式位 1"])
        self.io_level = QComboBox()
        self.io_level.addItems(["低电平 0", "高电平 1"])
        self.io_mode_button = QPushButton("写模式")
        self.io_level_button = QPushButton("写电平")
        self.io_mode_button.clicked.connect(self.write_io_mode)
        self.io_level_button.clicked.connect(self.write_io_level)
        self.led_index = QSpinBox()
        self.led_index.setRange(0, 1)
        self.led_color = QLineEdit("0x000000")
        self.led_button = QPushButton("写 LED RGB")
        self.led_button.clicked.connect(self.write_led)
        write_layout.addWidget(self.adc_write_unlock, 0, 0, 1, 6)
        write_layout.addWidget(QLabel("IO"), 1, 0)
        write_layout.addWidget(self.io_channel, 1, 1)
        write_layout.addWidget(self.io_mode, 1, 2)
        write_layout.addWidget(self.io_mode_button, 1, 3)
        write_layout.addWidget(self.io_level, 1, 4)
        write_layout.addWidget(self.io_level_button, 1, 5)
        write_layout.addWidget(QLabel("LED"), 2, 0)
        write_layout.addWidget(self.led_index, 2, 1)
        write_layout.addWidget(self.led_color, 2, 2, 1, 2)
        write_layout.addWidget(self.led_button, 2, 4, 1, 2)
        layout.addWidget(write_group)
        self._update_adc_write_controls()
        return page

    def _build_fan_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        info = QLabel(
            "风扇使用 GPIO18：0%/100% 为持续低/高电平，1–99% 使用 800 Hz PWM。"
            "连接 pigpiod 本身不会改变风扇状态。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        connect = QPushButton("连接本机 pigpiod")
        connect.clicked.connect(self.connect_fan)
        layout.addWidget(connect)
        self.fan_status = QLabel("未连接")
        layout.addWidget(self.fan_status)
        self.fan_unlock = QCheckBox("允许风扇转动")
        self.fan_unlock.toggled.connect(self.toggle_fan_unlock)
        layout.addWidget(self.fan_unlock)
        form = QFormLayout()
        self.fan_duty = QSpinBox()
        self.fan_duty.setRange(0, 100)
        self.fan_duty.setValue(30)
        form.addRow("占空比 (%)", self.fan_duty)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        apply_button = QPushButton("应用 PWM")
        apply_button.clicked.connect(self.apply_fan)
        stop_button = QPushButton("停止风扇")
        stop_button.setStyleSheet("font-weight: 700;")
        stop_button.clicked.connect(self.stop_fan)
        buttons.addWidget(apply_button)
        buttons.addWidget(stop_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _build_motor_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        warning = QLabel(
            "这里控制的是底盘有刷电机/驱动控制器。`cds_servo_*` 只是历史 ABI 名称，"
            "不代表物理执行器是舵机。7 号为左轮，8 号为右轮且方向取反。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #9a3412; font-weight: 600;")
        layout.addWidget(warning)

        bus = QHBoxLayout()
        open_button = QPushButton("打开 CDS 总线")
        open_button.clicked.connect(self.open_cds)
        read_button = QPushButton("读取 7/8 号位置（不转动）")
        read_button.clicked.connect(self.read_motor_positions)
        close_button = QPushButton("关闭总线")
        close_button.clicked.connect(self.close_cds)
        bus.addWidget(open_button)
        bus.addWidget(read_button)
        bus.addWidget(close_button)
        bus.addStretch(1)
        layout.addLayout(bus)
        self.motor_position = QLabel("ID7：—    ID8：—")
        layout.addWidget(self.motor_position)

        self.motor_unlock = QCheckBox("车轮已完全架空并固定，允许发送电机命令")
        self.motor_unlock.toggled.connect(self.toggle_motor_unlock)
        layout.addWidget(self.motor_unlock)

        command_group = QGroupBox("短脉冲验收（自动归零）")
        form = QFormLayout(command_group)
        self.left_speed = QSpinBox()
        self.left_speed.setRange(-100, 100)
        self.left_speed.setValue(0)
        self.right_speed = QSpinBox()
        self.right_speed.setRange(-100, 100)
        self.right_speed.setValue(0)
        self.pulse_ms = QSpinBox()
        self.pulse_ms.setRange(100, 1000)
        self.pulse_ms.setSingleStep(100)
        self.pulse_ms.setValue(300)
        form.addRow("左轮逻辑速度", self.left_speed)
        form.addRow("右轮逻辑速度", self.right_speed)
        form.addRow("持续时间 (ms)", self.pulse_ms)
        layout.addWidget(command_group)

        actions = QHBoxLayout()
        self.motor_mode_button = QPushButton("设置 7/8 为连续转动模式")
        self.motor_mode_button.clicked.connect(self.set_motor_mode)
        self.motor_pulse_button = QPushButton("执行短脉冲")
        self.motor_pulse_button.clicked.connect(self.run_motor_pulse)
        emergency = QPushButton("急停（7/8 归零）")
        emergency.setStyleSheet("background: #b91c1c; color: white; font-weight: 700;")
        emergency.clicked.connect(self.emergency_stop)
        actions.addWidget(self.motor_mode_button)
        actions.addWidget(self.motor_pulse_button)
        actions.addWidget(emergency)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._update_motor_controls()
        layout.addStretch(1)
        return page

    def _now(self) -> str:
        return dt.datetime.now().astimezone().isoformat(timespec="seconds")

    def log(self, module: str, message: str) -> None:
        event = {"time": self._now(), "module": module, "message": message}
        self.events.append(event)
        self.log_box.appendPlainText(f"[{event['time']}] {module}: {message}")

    def record(
        self,
        key: str,
        state: str,
        detail: str,
        call: CallResult | None = None,
    ) -> None:
        entry: dict[str, Any] = {"state": state, "detail": detail, "time": self._now()}
        if call is not None:
            entry.update(asdict(call))
            entry["error_text"] = call.error_text
        self.results[key] = entry
        self.log(key, f"{state} - {detail}")

    def _call(self, name: str, *args: Any) -> CallResult | None:
        if self.library is None:
            QMessageBox.warning(self, "尚未加载", "请先加载 libuptech.so。")
            return None
        try:
            result = self.library.call(name, *args)
        except Exception as exc:  # ctypes boundary: report instead of crashing UI
            self.log(name, f"调用异常: {exc}")
            QMessageBox.critical(self, "调用失败", f"{name}: {exc}")
            return None
        self.log(name, f"result={result.result}, errno={result.err} {result.error_text}")
        return result

    def _set_environment(self, key: str, state: str, detail: str) -> None:
        row = self.environment_rows[key]
        status_item = QTableWidgetItem(state)
        status_item.setForeground(QColor(STATUS_COLORS.get(state, "#111827")))
        self.environment_table.setItem(row, 1, status_item)
        self.environment_table.setItem(row, 2, QTableWidgetItem(detail))

    def browse_library(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 libuptech.so", self.library_path.text())
        if path:
            self.library_path.setText(path)

    def load_library(self) -> None:
        path = self.library_path.text().strip()
        try:
            self.library = UptechLibrary(path)
        except Exception as exc:
            self.library = None
            self._set_environment("library", "失败", str(exc))
            self._set_environment("abi", "失败", "未完成 ABI 检查")
            self.record("library.load", "失败", str(exc))
            return
        description = inspect_elf(self.library.path)
        self.library_path.setText(self.library.path)
        self._set_environment("library", "通过", description)
        self._set_environment(
            "abi", "通过", f"{len(CORE_SYMBOLS) + len(LCD_SYMBOLS)} 个兼容符号齐全"
        )
        self.record("library.load", "通过", f"{self.library.path} ({description})")
        self.record("abi.symbols", "通过", "核心与 LCD 兼容符号齐全")

    def refresh_environment(self) -> None:
        paths = {
            "i2c": os.environ.get("UPTECH_I2C_DEVICE", "/dev/i2c-1"),
            "spi": os.environ.get("UPTECH_SPI_DEVICE", "/dev/spidev1.0"),
            "uart": os.environ.get("UPTECH_UART_DEVICE", "/dev/ttyAMA0"),
            "gpio": os.environ.get("UPTECH_GPIOCHIP", "/dev/gpiochip4"),
        }
        for key, path in paths.items():
            exists = Path(path).exists()
            state = "通过" if exists else "失败"
            access = "存在" if exists else "不存在"
            if exists:
                access += ", 可读写" if os.access(path, os.R_OK | os.W_OK) else ", 权限不足"
            self._set_environment(key, state, f"{path}: {access}")
            self.results[f"environment.{key}"] = {
                "state": state,
                "detail": f"{path}: {access}",
                "time": self._now(),
            }
        try:
            with socket.create_connection(("localhost", 8888), timeout=0.25):
                pigpiod = True
        except OSError:
            pigpiod = False
        self._set_environment(
            "pigpiod", "通过" if pigpiod else "失败", "localhost:8888 可连接" if pigpiod else "未监听"
        )
        self.results["environment.pigpiod"] = {
            "state": "通过" if pigpiod else "失败",
            "detail": "localhost:8888",
            "time": self._now(),
        }

    def detect_mpu(self) -> bool:
        call = self._call("mpu6500_open")
        if call is None:
            return False
        passed = call.result == 0
        self.mpu_identity.setText("兼容芯片识别通过（0x70 或 0x68）" if passed else call.error_text)
        self.record(
            "mpu.identity",
            "通过" if passed else "失败",
            self.mpu_identity.text(),
            call,
        )
        return passed

    def initialise_mpu(self) -> None:
        answer = QMessageBox.question(
            self,
            "确认写寄存器",
            "该操作会配置 MPU6500 的时钟、滤波和量程。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        call = self._call("mpu6500_dmp_init")
        if call is not None:
            self.record(
                "mpu.initialise",
                "通过" if call.result == 0 else "失败",
                call.error_text,
                call,
            )

    def toggle_mpu_live(self, enabled: bool) -> None:
        if enabled:
            self.mpu_timer.start()
        else:
            self.mpu_timer.stop()

    def read_mpu(self) -> bool:
        vector_type = ctypes.c_float * 3
        accel = vector_type()
        gyro = vector_type()
        accel_call = self._call("mpu6500_Get_Accel", accel)
        gyro_call = self._call("mpu6500_Get_Gyro", gyro)
        if accel_call is None or gyro_call is None:
            return False
        passed = accel_call.result == 0 and gyro_call.result == 0
        if passed:
            self.accel_value.setText("  ".join(f"{value:+.4f}" for value in accel))
            self.gyro_value.setText("  ".join(f"{value:+.3f}" for value in gyro))
        else:
            self.accel_value.setText(accel_call.error_text)
            self.gyro_value.setText(gyro_call.error_text)

        gyro_fsr = ctypes.c_uint16()
        accel_fsr = ctypes.c_int8()
        gyro_fsr_call = self._call("mpu_get_gyro_fsr", ctypes.byref(gyro_fsr))
        accel_fsr_call = self._call("mpu_get_accel_fsr", ctypes.byref(accel_fsr))
        if gyro_fsr_call and accel_fsr_call and gyro_fsr_call.result == accel_fsr_call.result == 0:
            self.fsr_value.setText(f"±{accel_fsr.value} g / ±{gyro_fsr.value} °/s")
        self.record(
            "mpu.sample",
            "通过" if passed else "失败",
            f"accel={list(accel)}, gyro={list(gyro)}" if passed else "读取失败",
        )
        return passed

    def open_adc(self) -> bool:
        call = self._call("adc_io_open")
        if call is None:
            return False
        self.adc_opened = call.result >= 0
        self.record(
            "adc.open",
            "通过" if self.adc_opened else "失败",
            f"fd={call.result}" if self.adc_opened else call.error_text,
            call,
        )
        return self.adc_opened

    def close_adc(self) -> None:
        self.adc_timer.stop()
        self.adc_live.setChecked(False)
        if not self.adc_opened:
            return
        call = self._call("adc_io_close")
        if call is not None:
            self.record("adc.close", "通过" if call.result == 0 else "失败", call.error_text, call)
        self.adc_opened = False

    def toggle_adc_live(self, enabled: bool) -> None:
        if enabled:
            if not self.adc_opened and not self.open_adc():
                self.adc_live.setChecked(False)
                return
            self.adc_timer.start()
        else:
            self.adc_timer.stop()

    def read_adc(self) -> bool:
        if not self.adc_opened and not self.open_adc():
            return False
        values_type = ctypes.c_uint16 * 10
        values = values_type()
        values_call = self._call("ADC_GetAll", values)
        input_call = self._call("adc_io_InputGetAll")
        mode = ctypes.c_uint8()
        mode_call = self._call("adc_io_ModeGetAll", ctypes.byref(mode))
        if values_call is None or input_call is None or mode_call is None:
            return False
        passed = values_call.result == 0 and input_call.result >= 0 and mode_call.result == 0
        if passed:
            for index, value in enumerate(values):
                self.adc_table.item(index, 1).setText(str(value))
            self.io_value.setText(
                f"输入掩码：0x{input_call.result:02X} ({input_call.result:08b})    "
                f"模式掩码：0x{mode.value:02X} ({mode.value:08b})"
            )
        self.record(
            "adc.sample",
            "通过" if passed else "失败",
            f"values={list(values)}, input=0x{input_call.result & 0xff:02X}, mode=0x{mode.value:02X}"
            if passed
            else "ADC/IO 读取失败",
        )
        return passed

    def toggle_adc_write_unlock(self, enabled: bool) -> None:
        if enabled:
            answer = QMessageBox.warning(
                self,
                "确认输出写操作",
                "错误的 IO 模式或电平可能与外部硬件冲突。确认已核对接线？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.adc_write_unlock.blockSignals(True)
                self.adc_write_unlock.setChecked(False)
                self.adc_write_unlock.blockSignals(False)
        self._update_adc_write_controls()

    def _update_adc_write_controls(self) -> None:
        enabled = self.adc_write_unlock.isChecked()
        for widget in (self.io_mode_button, self.io_level_button, self.led_button):
            widget.setEnabled(enabled)

    def _ensure_adc_write(self) -> bool:
        if not self.adc_write_unlock.isChecked():
            QMessageBox.warning(self, "写操作已锁定", "请先确认接线并解锁 ADC/IO 写操作。")
            return False
        return self.adc_opened or self.open_adc()

    def write_io_mode(self) -> None:
        if not self._ensure_adc_write():
            return
        call = self._call("adc_io_ModeSet", self.io_channel.value(), self.io_mode.currentIndex())
        if call:
            self.record("adc.write_mode", "通过" if call.result == 0 else "失败", call.error_text, call)

    def write_io_level(self) -> None:
        if not self._ensure_adc_write():
            return
        call = self._call("adc_io_Set", self.io_channel.value(), self.io_level.currentIndex())
        if call:
            self.record("adc.write_level", "通过" if call.result == 0 else "失败", call.error_text, call)

    def write_led(self) -> None:
        if not self._ensure_adc_write():
            return
        try:
            color = int(self.led_color.text().strip(), 0)
        except ValueError:
            QMessageBox.warning(self, "颜色无效", "请输入如 0x00FF00 的 24 位 RGB 整数。")
            return
        if not 0 <= color <= 0xFFFFFF:
            QMessageBox.warning(self, "颜色无效", "RGB 必须在 0x000000 到 0xFFFFFF 之间。")
            return
        call = self._call("adc_led_set", self.led_index.value(), color)
        if call:
            self.record("adc.write_led", "通过" if call.result == 0 else "失败", call.error_text, call)

    def connect_fan(self) -> bool:
        if self.fan_pi is not None and getattr(self.fan_pi, "connected", False):
            return True
        try:
            import pigpio  # type: ignore

            pigpio.exceptions = False
            connection = pigpio.pi("localhost", 8888)
        except Exception as exc:
            self.fan_status.setText(f"连接失败: {exc}")
            self.record("fan.connect", "失败", str(exc))
            return False
        if not connection.connected:
            connection.stop()
            self.fan_status.setText("连接失败：pigpiod 未运行")
            self.record("fan.connect", "失败", "pigpiod 未运行或不可连接")
            return False
        self.fan_pi = connection
        self.fan_status.setText("已连接；GPIO18 尚未写入")
        self.record("fan.connect", "通过", "pigpiod localhost:8888")
        return True

    def toggle_fan_unlock(self, enabled: bool) -> None:
        if enabled:
            answer = QMessageBox.question(self, "确认风扇安全", "确认风扇叶片无遮挡，可以转动？")
            if answer != QMessageBox.StandardButton.Yes:
                self.fan_unlock.blockSignals(True)
                self.fan_unlock.setChecked(False)
                self.fan_unlock.blockSignals(False)

    def apply_fan(self) -> None:
        duty = self.fan_duty.value()
        if duty > 0 and not self.fan_unlock.isChecked():
            QMessageBox.warning(self, "风扇已锁定", "请先确认叶片安全并勾选允许风扇转动。")
            return
        if not self.connect_fan():
            return
        if duty == 0:
            result = int(self.fan_pi.write(18, 0))
            detail = "GPIO18 持续低电平 / 0%"
        elif duty == 100:
            result = int(self.fan_pi.write(18, 1))
            detail = "GPIO18 持续高电平 / 100%"
        else:
            range_result = int(self.fan_pi.set_PWM_range(18, 100))
            actual_frequency = int(self.fan_pi.set_PWM_frequency(18, 800))
            duty_result = int(self.fan_pi.set_PWM_dutycycle(18, duty))
            result = range_result if range_result < 0 else duty_result
            detail = f"GPIO18 / {actual_frequency} Hz / {duty}%"
        passed = result == 0
        if passed:
            self.fan_output_owned = True
        self.fan_status.setText(detail if passed else f"pigpio 错误 {result}")
        self.record("fan.pwm", "通过" if passed else "失败", self.fan_status.text())

    def stop_fan(self) -> None:
        if not self.connect_fan():
            return
        result = int(self.fan_pi.write(18, 0))
        if result == 0:
            self.fan_output_owned = True
        self.fan_status.setText("GPIO18 持续低电平 / 0%（已停止）")
        self.record("fan.stop", "通过" if result == 0 else "失败", f"pigpio result={result}")

    def open_cds(self) -> bool:
        call = self._call("cds_servo_open")
        if call is None:
            return False
        self.cds_opened = call.result >= 0
        self.record(
            "motor.bus_open",
            "通过" if self.cds_opened else "失败",
            f"fd={call.result}" if self.cds_opened else call.error_text,
            call,
        )
        return self.cds_opened

    def close_cds(self) -> None:
        if not self.cds_opened:
            return
        self.emergency_stop()
        call = self._call("cds_servo_close")
        if call:
            self.record("motor.bus_close", "通过" if call.result == 0 else "失败", call.error_text, call)
        self.cds_opened = False

    def read_motor_positions(self) -> bool:
        if not self.cds_opened and not self.open_cds():
            return False
        left = self._call("cds_servo_GetPos", 7)
        right = self._call("cds_servo_GetPos", 8)
        if left is None or right is None:
            return False
        passed = left.result >= 0 and right.result >= 0
        unsupported = (
            left.result < 0
            and right.result < 0
            and left.err == errno.EIO
            and right.err == errno.EIO
        )
        self.motor_position.setText(f"ID7：{left.result}    ID8：{right.result}")
        self.record(
            "motor.position_read",
            "通过" if passed else "不支持" if unsupported else "失败",
            f"ID7={left.result}, ID8={right.result}; 某些有刷控制器可能不提供位置反馈",
        )
        return passed or unsupported

    def toggle_motor_unlock(self, enabled: bool) -> None:
        if enabled:
            answer = QMessageBox.warning(
                self,
                "确认机械安全",
                "非零命令会让底盘有刷电机真实转动。确认车轮已完全架空并固定？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.motor_unlock.blockSignals(True)
                self.motor_unlock.setChecked(False)
                self.motor_unlock.blockSignals(False)
        else:
            self.emergency_stop(silent=True)
        self._update_motor_controls()

    def _update_motor_controls(self) -> None:
        enabled = self.motor_unlock.isChecked() and not self.motor_pulse_active
        self.motor_mode_button.setEnabled(enabled)
        self.motor_pulse_button.setEnabled(enabled)

    def set_motor_mode(self) -> None:
        if not self.motor_unlock.isChecked():
            return
        if not self.cds_opened and not self.open_cds():
            return
        left = self._call("cds_servo_SetMode", 7, 1)
        right = self._call("cds_servo_SetMode", 8, 1)
        passed = bool(left and right and left.result == 0 and right.result == 0)
        self.record("motor.mode", "通过" if passed else "失败", "ID7/ID8 连续转动模式")

    def run_motor_pulse(self) -> None:
        if not self.motor_unlock.isChecked() or self.motor_pulse_active:
            return
        if not self.cds_opened and not self.open_cds():
            return
        left_speed = self.left_speed.value()
        right_speed = self.right_speed.value()
        if left_speed == 0 and right_speed == 0:
            QMessageBox.information(self, "速度为零", "请设置至少一个非零速度，或直接使用急停。")
            return
        self.motor_pulse_active = True
        self._update_motor_controls()
        left = self._call("cds_servo_SetSpeed", 7, left_speed)
        right = self._call("cds_servo_SetSpeed", 8, -right_speed)
        passed = bool(left and right and left.result == 0 and right.result == 0)
        self.record(
            "motor.pulse_start",
            "通过" if passed else "失败",
            f"left={left_speed}, right={right_speed}, duration={self.pulse_ms.value()}ms",
        )
        if not passed:
            self.emergency_stop()
            return
        self.motor_stop_timer.start(self.pulse_ms.value())

    def emergency_stop(self, silent: bool = False) -> None:
        self.motor_stop_timer.stop()
        if self.library is not None and self.cds_opened:
            left = self.library.call("cds_servo_SetSpeed", 7, 0)
            right = self.library.call("cds_servo_SetSpeed", 8, 0)
            passed = left.result == 0 and right.result == 0
            self.record("motor.stop", "通过" if passed else "失败", "ID7=0, ID8=0")
        self.motor_pulse_active = False
        self._update_motor_controls()
        if not silent:
            self.statusBar().showMessage("底盘电机已发送零速命令", 3000)

    def run_read_only_suite(self) -> None:
        if self.library is None:
            self.load_library()
        if self.library is None:
            return
        self.log("suite", "开始只读验收；不启动风扇、不发送非零电机速度")
        self.refresh_environment()
        QApplication.processEvents()
        self.detect_mpu()
        self.read_mpu()
        QApplication.processEvents()
        self.open_adc()
        self.read_adc()
        QApplication.processEvents()
        self.open_cds()
        self.read_motor_positions()
        self.connect_fan()
        self.record("mpu.attitude", "不支持", "兼容库当前返回 ENOTSUP")
        self.log("suite", "只读验收完成")

    def export_report(self) -> None:
        suggested = f"openlibuptech-acceptance-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        path, _ = QFileDialog.getSaveFileName(self, "导出验收报告", suggested, "JSON (*.json)")
        if not path:
            return
        report = {
            "generated_at": self._now(),
            "application": "OpenLibUptech acceptance_gui",
            "host": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "library": self.library.path if self.library else self.library_path.text(),
            "library_elf": inspect_elf(self.library.path) if self.library else None,
            "results": self.results,
            "events": self.events,
            "safety": {
                "adc_write_unlocked": self.adc_write_unlock.isChecked(),
                "fan_unlocked": self.fan_unlock.isChecked(),
                "motor_unlocked": self.motor_unlock.isChecked(),
                "motor_speed_limit": 100,
                "motor_pulse_limit_ms": 1000,
            },
        }
        try:
            Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.log("report", f"已导出 {path}")
        self.statusBar().showMessage(f"报告已保存：{path}", 5000)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        self.mpu_timer.stop()
        self.adc_timer.stop()
        self.motor_stop_timer.stop()
        try:
            self.emergency_stop(silent=True)
        except Exception:
            pass
        try:
            if self.adc_opened and self.library:
                self.library.call("adc_io_close")
        except Exception:
            pass
        try:
            if self.cds_opened and self.library:
                self.library.call("cds_servo_close")
        except Exception:
            pass
        try:
            if self.fan_pi is not None and self.fan_output_owned:
                self.fan_pi.write(18, 0)
            if self.fan_pi is not None:
                self.fan_pi.stop()
        except Exception:
            pass
        event.accept()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", nargs="?", help="path to the AArch64 libuptech.so")
    args = parser.parse_args()
    application = QApplication(sys.argv)
    application.setApplicationName("OpenLibUptech Acceptance")
    window = AcceptanceWindow(initial_library_path(args.library))
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
