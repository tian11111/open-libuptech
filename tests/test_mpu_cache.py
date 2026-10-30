"""Compile and run the hardware-free Linux MPU/UART regression harness."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux headers")
class MpuCacheTests(unittest.TestCase):
    def test_mock_sensor_concurrency_fork_and_attitude(self):
        source = Path(__file__).with_suffix(".c")
        with tempfile.TemporaryDirectory(prefix="uptech-mpu-") as directory:
            executable = str(Path(directory) / "mpu-regression")
            command = shlex.split(os.environ.get("CC", "gcc")) + [
                "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                str(source), "-lm", "-o", executable,
            ]
            subprocess.run(command, check=True, timeout=30)
            subprocess.run([executable], check=True, timeout=15)


if __name__ == "__main__":
    unittest.main()
