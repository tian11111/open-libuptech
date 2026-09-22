#!/usr/bin/env python3
"""CPU-temperature-controlled GPIO18 fan service for OpenLibUptech."""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

try:  # unavailable on Windows, where the pure unit tests also run
    import grp
except ImportError:  # pragma: no cover - exercised implicitly on Windows
    grp = None  # type: ignore[assignment]

GPIO = 18
PWM_FREQUENCY = 800
PWM_RANGE = 100
DEFAULT_SOCKET = "/run/openlibuptech/fan.sock"


def duty_for_temperature(temp_c: Optional[float]) -> int:
    """Return the safe fan duty for a temperature, without hysteresis."""
    if temp_c is None:
        return 100
    if temp_c < 45:
        return 20
    if temp_c < 55:
        return 70
    if temp_c < 65:
        return 80
    if temp_c < 75:
        return 90
    return 100


def read_cpu_temperature() -> float:
    return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0


@dataclass
class FanState:
    mode: str = "auto"
    duty: int = 100
    temperature_c: Optional[float] = None
    manual_until: Optional[float] = None


class FanController:
    """Hardware-independent logic; accepts a pigpio-like object for tests."""

    _DUTIES = (20, 70, 80, 90, 100)
    _UP_THRESHOLDS = (45, 55, 65, 75)

    def __init__(self, pi: Any, thermal_reader: Callable[[], float] = read_cpu_temperature,
                 clock: Callable[[], float] = time.monotonic, lease_seconds: float = 3.0) -> None:
        self.pi, self.thermal_reader, self.clock, self.lease_seconds = pi, thermal_reader, clock, lease_seconds
        self.state = FanState()
        self._band: Optional[int] = None

    @staticmethod
    def _check(result: Any, action: str) -> None:
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"pigpio {action} failed: {result}")

    def configure(self) -> None:
        self._check(self.pi.set_PWM_frequency(GPIO, PWM_FREQUENCY), "set frequency")
        self._check(self.pi.set_PWM_range(GPIO, PWM_RANGE), "set range")

    def set_duty(self, duty: int) -> None:
        duty = max(0, min(PWM_RANGE, int(duty)))
        self._check(self.pi.set_PWM_dutycycle(GPIO, duty), "set duty")
        self.state.duty = duty

    def startup(self) -> None:
        self.configure()
        self.set_duty(100)

    def _hysteretic_band(self, temp_c: float) -> int:
        if self._band is None:
            self._band = min(4, sum(temp_c >= threshold for threshold in self._UP_THRESHOLDS))
            return self._band
        # Cross each boundary by 2 C before changing its fan band.
        while self._band < 4 and temp_c >= self._UP_THRESHOLDS[self._band] + 2:
            self._band += 1
        while self._band > 0 and temp_c < self._UP_THRESHOLDS[self._band - 1] - 2:
            self._band -= 1
        return self._band

    def set_auto(self) -> None:
        self.state.mode, self.state.manual_until = "auto", None
        self.tick()

    def set_manual(self, duty: Any) -> None:
        if isinstance(duty, bool) or not isinstance(duty, (int, float)) or not 0 <= duty <= 100:
            raise ValueError("manual duty must be a number from 0 to 100")
        self.state.mode = "manual"
        self.state.manual_until = self.clock() + self.lease_seconds
        self.set_duty(int(duty))

    def keepalive(self) -> None:
        if self.state.mode != "manual":
            raise ValueError("manual mode is not active")
        self.state.manual_until = self.clock() + self.lease_seconds

    def tick(self) -> None:
        now = self.clock()
        if self.state.mode == "manual":
            if self.state.manual_until is not None and now < self.state.manual_until:
                return
            self.state.mode, self.state.manual_until = "auto", None
        try:
            temp_c = float(self.thermal_reader())
            if temp_c != temp_c:
                raise ValueError("temperature is NaN")
            self.state.temperature_c = temp_c
            duty = self._DUTIES[self._hysteretic_band(temp_c)]
        except Exception:
            self.state.temperature_c, self._band, duty = None, None, 100
        self.set_duty(duty)

    def status(self) -> dict[str, Any]:
        remaining = 0.0 if self.state.manual_until is None else max(0.0, self.state.manual_until - self.clock())
        return {"ok": True, "mode": self.state.mode, "temperature_c": self.state.temperature_c,
                "duty": self.state.duty, "pwm_frequency": PWM_FREQUENCY, "pwm_range": PWM_RANGE,
                "manual_lease_remaining": round(remaining, 2)}

    def command(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        command = request.get("command")
        if command == "status":
            return self.status()
        if command == "auto":
            self.set_auto()
        elif command == "manual":
            self.set_manual(request.get("duty"))
        elif command == "keepalive":
            self.keepalive()
        else:
            raise ValueError("command must be status, auto, manual, or keepalive")
        return self.status()

    def shutdown(self) -> None:
        for method, args in (("write", (GPIO, 0)), ("stop", ())):
            try:
                getattr(self.pi, method)(*args)
            except Exception:
                pass


class FanSocketServer:
    def __init__(self, controller: FanController, socket_path: str) -> None:
        self.controller, self.socket_path, self.server = controller, socket_path, None

    def open(self) -> None:
        os.makedirs(os.path.dirname(self.socket_path), mode=0o755, exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o660)
        try:
            if grp is not None:
                os.chown(self.socket_path, -1, grp.getgrnam("gpio").gr_gid)
        except (KeyError, PermissionError):  # development host without gpio group
            pass
        self.server.listen(8)
        self.server.setblocking(False)

    def serve_once(self, timeout: float) -> None:
        if self.server is None:
            raise RuntimeError("socket server not open")
        ready, _, _ = select.select([self.server], [], [], timeout)
        if ready:
            client, _ = self.server.accept()
            with client:
                client.settimeout(1.0)
                try:
                    request = json.loads(client.recv(4096).decode("utf-8").split("\n", 1)[0])
                    response = self.controller.command(request)
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                client.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
        self.controller.tick()

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="OpenLibUptech CPU-temperature fan controller")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.lease_seconds <= 0:
        parser.error("interval and lease-seconds must be positive")
    try:
        import pigpio  # type: ignore
        pigpio.exceptions = False
        pi = pigpio.pi("localhost", 8888)
        if not pi.connected:
            raise RuntimeError("cannot connect to pigpiod")
        controller = FanController(pi, lease_seconds=args.lease_seconds)
        controller.startup()
    except Exception as exc:
        print(f"uptech fan controller startup failed: {exc}", file=sys.stderr)
        return 1
    server = FanSocketServer(controller, args.socket)
    try:
        server.open()
        time.sleep(1.0)  # a reliable full-speed startup kick
        controller.tick()
        while True:
            server.serve_once(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"uptech fan controller failed: {exc}", file=sys.stderr)
        return 1
    finally:
        server.close()
        controller.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
