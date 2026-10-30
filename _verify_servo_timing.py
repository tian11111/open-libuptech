"""Measure real /dev/ttyAMA0 send latency with a zero-speed frame.

A zero-speed command keeps both motors stopped; the frame is a valid no-op
that still exercises the full transmit path.  Keep the wheels off the ground.
"""

import ctypes
import statistics
import sys
import time

library = ctypes.CDLL(sys.argv[1], use_errno=True)
library.cds_servo_open.restype = ctypes.c_int
library.cds_servo_close.restype = ctypes.c_int
library.cds_servo_SetSpeed.argtypes = [ctypes.c_int, ctypes.c_int]
library.cds_servo_SetSpeed.restype = ctypes.c_int
library.cds_servo_ReadReg.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int
]
library.cds_servo_ReadReg.restype = ctypes.c_int


def main():
    if library.cds_servo_open() < 0:
        raise SystemExit(f"cds_servo_open failed, errno={ctypes.get_errno()}")
    try:
        # Explicit zero speed on both wheels before timing anything.
        for wheel in (7, 8):
            assert library.cds_servo_SetSpeed(wheel, 0) == 0, f"wheel {wheel} stop failed"
        time.sleep(0.2)

        samples = []
        for _ in range(300):
            start = time.perf_counter()
            result = library.cds_servo_SetSpeed(7, 0)
            samples.append((time.perf_counter() - start) * 1e6)
            assert result == 0, f"send failed, errno={ctypes.get_errno()}"
        samples.sort()
        print(f"SendFrame (zero speed) p50={statistics.median(samples):.1f} us "
              f"p99={samples[int(len(samples) * 0.99)]:.1f} us "
              f"max={samples[-1]:.1f} us")

        for wheel in (7, 8):
            assert library.cds_servo_SetSpeed(wheel, 0) == 0
        print("OK")
    finally:
        library.cds_servo_close()


if __name__ == "__main__":
    main()
