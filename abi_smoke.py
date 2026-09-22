import ctypes
import sys


library = ctypes.CDLL(sys.argv[1], use_errno=True)

required = [
    "adc_io_open",
    "adc_io_close",
    "ADC_GetAll",
    "adc_io_InputGetAll",
    "adc_io_Set",
    "adc_io_SetAll",
    "adc_io_ModeGetAll",
    "adc_io_ModeSet",
    "adc_io_ModeSetAll",
    "adc_led_set",
    "cds_servo_open",
    "cds_servo_close",
    "cds_servo_SetMode",
    "cds_servo_SetAngle",
    "cds_servo_SetSpeed",
    "cds_servo_GetPos",
    "mpu6500_dmp_init",
    "mpu6500_Get_Accel",
    "mpu6500_Get_Gyro",
    "mpu6500_Get_Attitude",
]

missing = [name for name in required if not hasattr(library, name)]
if missing:
    raise SystemExit(f"missing symbols: {missing}")

print(f"loaded {sys.argv[1]}; required ABI symbols present")
