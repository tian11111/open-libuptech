"""Hardware-free regression for FIFO-empty versus software-queue-empty timing."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux headers")
class UartTxTests(unittest.TestCase):
    def test_direction_covers_complete_frame(self):
        with tempfile.TemporaryDirectory(prefix="uptech-uart-") as directory:
            executable = str(Path(directory) / "uart-regression")
            command = shlex.split(os.environ.get("CC", "gcc")) + [
                "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                str(Path(__file__).with_suffix(".c")), "-lm", "-o", executable,
            ]
            subprocess.run(command, check=True, timeout=30)
            subprocess.run([executable], check=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
