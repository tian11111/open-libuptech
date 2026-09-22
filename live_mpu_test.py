import ctypes
import errno
import sys
import time


library_path = sys.argv[1] if len(sys.argv) > 1 else "./libuptech.so"
library = ctypes.CDLL(library_path, use_errno=True)

vector_type = ctypes.c_float * 3
for name in ("mpu6500_Get_Accel", "mpu6500_Get_Gyro", "mpu6500_Get_Attitude"):
    function = getattr(library, name)
    function.argtypes = [ctypes.POINTER(ctypes.c_float)]
    function.restype = ctypes.c_int

library.mpu6500_open.restype = ctypes.c_int
library.mpu6500_dmp_init.restype = ctypes.c_int


def checked_call(name, *args):
    ctypes.set_errno(0)
    result = getattr(library, name)(*args)
    error = ctypes.get_errno()
    error_name = errno.errorcode.get(error, "OK") if error else "OK"
    return result, error, error_name


print("open", *checked_call("mpu6500_open"))
print("init", *checked_call("mpu6500_dmp_init"))

for index in range(5):
    accel = vector_type()
    gyro = vector_type()
    accel_status = checked_call("mpu6500_Get_Accel", accel)
    gyro_status = checked_call("mpu6500_Get_Gyro", gyro)
    print(
        "sample",
        index,
        "accel_g",
        *[round(value, 4) for value in accel],
        "gyro_dps",
        *[round(value, 3) for value in gyro],
        "status",
        accel_status,
        gyro_status,
    )
    time.sleep(0.1)

attitude = vector_type()
print("attitude", *checked_call("mpu6500_Get_Attitude", attitude), list(attitude))
