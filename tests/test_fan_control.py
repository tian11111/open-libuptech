import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uptech_fan_control import FanController, FanSocketServer, duty_for_temperature


class FakePigpio:
    def __init__(self): self.calls, self.fail_duty = [], False
    def set_PWM_frequency(self, gpio, frequency): self.calls.append(("frequency", gpio, frequency)); return frequency
    def set_PWM_range(self, gpio, value): self.calls.append(("range", gpio, value)); return value
    def set_PWM_dutycycle(self, gpio, duty): self.calls.append(("duty", gpio, duty)); return -1 if self.fail_duty else 0
    def write(self, gpio, value): self.calls.append(("write", gpio, value)); return 0
    def stop(self): self.calls.append(("stop",))


class FanControlTests(unittest.TestCase):
    def setUp(self):
        self.now, self.temperature, self.pi = [0.0], [44.0], FakePigpio()
        self.controller = FanController(self.pi, thermal_reader=lambda: self.temperature[0], clock=lambda: self.now[0], lease_seconds=3)

    def test_curve_boundaries(self):
        self.assertEqual([duty_for_temperature(t) for t in (44.9, 45, 54.9, 55, 64.9, 65, 74.9, 75)], [20, 70, 70, 80, 80, 90, 90, 100])
        self.assertEqual(duty_for_temperature(None), 100)

    def test_hysteresis(self):
        self.controller.tick(); self.assertEqual(self.controller.state.duty, 20)
        self.temperature[0] = 45; self.controller.tick(); self.assertEqual(self.controller.state.duty, 20)
        self.temperature[0] = 47; self.controller.tick(); self.assertEqual(self.controller.state.duty, 70)
        self.temperature[0] = 44; self.controller.tick(); self.assertEqual(self.controller.state.duty, 70)
        self.temperature[0] = 42.9; self.controller.tick(); self.assertEqual(self.controller.state.duty, 20)

    def test_sensor_and_pigpio_failure_are_visible(self):
        bad = FanController(self.pi, thermal_reader=lambda: (_ for _ in ()).throw(OSError("missing")))
        bad.tick(); self.assertEqual(bad.state.duty, 100); self.assertIsNone(bad.state.temperature_c)
        self.pi.fail_duty = True
        with self.assertRaises(RuntimeError): self.controller.tick()

    def test_manual_lease_reverts_to_auto(self):
        self.controller.set_manual(25); self.assertEqual(self.controller.state.mode, "manual")
        self.now[0] = 2; self.controller.keepalive(); self.now[0] = 4.9; self.controller.tick(); self.assertEqual(self.controller.state.mode, "manual")
        self.now[0] = 5.1; self.controller.tick(); self.assertEqual(self.controller.state.mode, "auto"); self.assertEqual(self.controller.state.duty, 20)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix-domain sockets are unavailable on this host")
    def test_json_socket_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            server = FanSocketServer(self.controller, str(Path(directory) / "fan.sock")); server.open()
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); client.connect(server.socket_path)
                client.sendall(b'{"command":"manual","duty":31}\n'); server.serve_once(0)
                response = json.loads(client.recv(4096).decode()); client.close()
                self.assertTrue(response["ok"]); self.assertEqual(response["mode"], "manual"); self.assertEqual(response["duty"], 31)
            finally: server.close()


if __name__ == "__main__": unittest.main()
