"""Offscreen motor controls with a fake library; never opens a device."""

import errno
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except ImportError:
    QApplication = None
else:
    import acceptance_gui as gui


class FakeLibrary:
    def __init__(self, failed_wheel=None, raises=False):
        self.calls = []
        self.failed_wheel = failed_wheel
        self.raises = raises

    def call(self, name, wheel, speed):
        assert name == "cds_servo_SetSpeed"
        self.calls.append((wheel, speed))
        if wheel == self.failed_wheel and speed == 0:
            if self.raises:
                raise OSError("injected stop error")
            return gui.CallResult(name, -1, errno.EIO)
        return gui.CallResult(name, 0, 0)


@unittest.skipIf(QApplication is None, "PyQt6 is optional")
class MotorGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(gui.AcceptanceWindow, "refresh_environment"):
            self.window = gui.AcceptanceWindow("/__uptech_no_hardware_library__")
        self.fake = FakeLibrary()
        self.window.library = self.fake
        self.window.cds_opened = True
        self.window.motor_unlock.blockSignals(True)
        self.window.motor_unlock.setChecked(True)
        self.window.motor_unlock.blockSignals(False)

    def tearDown(self):
        self.window.motor_stop_timer.stop()
        self.window.cds_opened = False
        self.window.library = None
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()

    def test_right_max_pulse_then_both_zero(self):
        self.window.left_speed.setValue(0)
        self.window.right_speed.setValue(1024)
        self.window.pulse_ms.setValue(100)
        self.window.run_motor_pulse()
        self.assertEqual(self.fake.calls, [(7, 0), (8, -1023)])
        QTest.qWait(250)
        self.assertEqual(self.fake.calls, [(7, 0), (8, -1023), (7, 0), (8, 0)])
        self.assertFalse(self.window.motor_pulse_active)
        self.assertFalse(self.window.motor_stop_timer.isActive())
        self.assertEqual(self.window.results["motor.stop"]["state"], "通过")
        self.assertIn("无停转反馈", self.window.motor_stop_status.text())
        QTest.qWait(150)
        self.assertEqual(len(self.fake.calls), 4)

    def test_both_max_pulse_then_both_zero(self):
        self.window.left_speed.setValue(1024)
        self.window.right_speed.setValue(1024)
        self.window.pulse_ms.setValue(100)
        self.window.run_motor_pulse()
        QTest.qWait(250)
        self.assertEqual(self.fake.calls, [(7, 1023), (8, -1023), (7, 0), (8, 0)])

    def test_stop_failure_still_attempts_both_and_locks_controls(self):
        for wheel in (7, 8):
            for raises in (False, True):
                with self.subTest(wheel=wheel, raises=raises):
                    self.fake = FakeLibrary(wheel, raises)
                    self.window.library = self.fake
                    self.window.emergency_stop()
                    self.assertEqual(self.fake.calls, [(7, 0), (8, 0)])
                    self.assertEqual(self.window.results["motor.stop"]["state"], "失败")
                    self.assertIn("停转未确认", self.window.statusBar().currentMessage())
                    self.assertIn("#b91c1c", self.window.motor_stop_status.styleSheet())
                    self.assertFalse(self.window.motor_unlock.isChecked())
                    self.assertFalse(self.window.motor_pulse_button.isEnabled())
                    side = "left" if wheel == 7 else "right"
                    result = self.window.results[f"motor.stop.{side}"]
                    if not raises:
                        self.assertEqual(result["result"], -1)
                        self.assertEqual(result["err"], errno.EIO)


if __name__ == "__main__":
    unittest.main()
