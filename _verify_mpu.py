"""MPU verification: cache latency, FSR restoration, and attitude output."""

import ctypes
import errno
import os
import statistics
import sys
import time

library = ctypes.CDLL(sys.argv[1], use_errno=True)

Float3 = ctypes.c_float * 3

library.mpu6500_open.restype = ctypes.c_int
library.mpu6500_Get_Accel.argtypes = [ctypes.POINTER(ctypes.c_float)]
library.mpu6500_Get_Accel.restype = ctypes.c_int
library.mpu6500_Get_Gyro.argtypes = [ctypes.POINTER(ctypes.c_float)]
library.mpu6500_Get_Gyro.restype = ctypes.c_int
library.mpu6500_Get_Attitude.argtypes = [ctypes.POINTER(ctypes.c_float)]
library.mpu6500_Get_Attitude.restype = ctypes.c_int
library.mpu_get_accel_fsr.argtypes = [ctypes.POINTER(ctypes.c_int8)]
library.mpu_get_accel_fsr.restype = ctypes.c_int
library.mpu_get_gyro_fsr.argtypes = [ctypes.POINTER(ctypes.c_uint16)]
library.mpu_get_gyro_fsr.restype = ctypes.c_int
library.mpu_set_accel_fsr.argtypes = [ctypes.c_int]
library.mpu_set_accel_fsr.restype = ctypes.c_int
library.mpu_set_gyro_fsr.argtypes = [ctypes.c_uint]
library.mpu_set_gyro_fsr.restype = ctypes.c_int


def timed(call, rounds=200):
    samples = []
    for _ in range(rounds):
        start = time.perf_counter_ns()
        call()
        samples.append((time.perf_counter_ns() - start) / 1000.0)
    return statistics.median(samples), max(samples)


def get_fsrs():
    accel = ctypes.c_int8()
    gyro = ctypes.c_uint16()
    assert library.mpu_get_accel_fsr(ctypes.byref(accel)) == 0
    assert library.mpu_get_gyro_fsr(ctypes.byref(gyro)) == 0
    return accel.value, gyro.value


def main():
    interval = os.environ.get("UPTECH_MPU_CACHE_MS", "10")
    if library.mpu6500_open() != 0:
        raise SystemExit(f"mpu6500_open failed, errno={ctypes.get_errno()}")

    initial_accel_fsr, initial_gyro_fsr = get_fsrs()
    try:
        time.sleep(0.3)  # let the sampling thread publish its first frames

        accel = Float3()
        gyro = Float3()
        assert library.mpu6500_Get_Accel(accel) == 0
        assert library.mpu6500_Get_Gyro(gyro) == 0
        print(f"cache_ms={interval}")
        print(f"accel  = [{accel[0]:+.3f} {accel[1]:+.3f} {accel[2]:+.3f}] g")
        print(f"gyro   = [{gyro[0]:+.3f} {gyro[1]:+.3f} {gyro[2]:+.3f}] dps")
        magnitude = (accel[0] ** 2 + accel[1] ** 2 + accel[2] ** 2) ** 0.5
        print(f"|accel| = {magnitude:.3f} g (expect ~1.0)")

        median, worst = timed(lambda: library.mpu6500_Get_Accel(accel))
        print(f"Get_Accel median {median:.1f} us, max {worst:.1f} us")
        median, worst = timed(lambda: library.mpu6500_Get_Gyro(gyro))
        print(f"Get_Gyro  median {median:.1f} us, max {worst:.1f} us")

        print(f"fsr initial: accel +-{initial_accel_fsr} g, "
              f"gyro +-{initial_gyro_fsr} dps")

        assert library.mpu_set_accel_fsr(2) == 0
        assert get_fsrs()[0] == 2
        time.sleep(0.3)
        assert library.mpu6500_Get_Accel(accel) == 0
        after = (accel[0] ** 2 + accel[1] ** 2 + accel[2] ** 2) ** 0.5
        print(f"+-2g magnitude {after:.3f} g")
        assert 0.9 < after < 1.1, after

        assert library.mpu_set_gyro_fsr(250) == 0
        assert get_fsrs()[1] == 250

        attitude = Float3()
        if int(interval) <= 0:
            result = library.mpu6500_Get_Attitude(attitude)
            error = ctypes.get_errno()
            print(f"synchronous attitude rc={result} errno={error} (expect ENOTSUP)")
            assert result == -1 and error == errno.ENOTSUP
        else:
            deadline = time.monotonic() + 1.0
            while True:
                result = library.mpu6500_Get_Attitude(attitude)
                error = ctypes.get_errno()
                if result == 0 or time.monotonic() >= deadline:
                    break
                assert error == errno.EAGAIN, error
                time.sleep(0.01)
            print(f"Get_Attitude rc={result} errno={error} "
                  f"[pitch={attitude[0]:+.2f} roll={attitude[1]:+.2f} "
                  f"yaw={attitude[2]:+.2f}]")
            assert result == 0, f"attitude unavailable, errno={error}"
    finally:
        assert library.mpu_set_accel_fsr(initial_accel_fsr) == 0
        assert library.mpu_set_gyro_fsr(initial_gyro_fsr) == 0
        restored_accel, restored_gyro = get_fsrs()
        assert (restored_accel, restored_gyro) == (
            initial_accel_fsr, initial_gyro_fsr
        )
        print(f"fsr restored: accel +-{restored_accel} g, "
              f"gyro +-{restored_gyro} dps")

    print("OK")


if __name__ == "__main__":
    main()
