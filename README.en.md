# OpenLibUptech

[中文](README.md) · [English](README.en.md) · [Full tutorial](docs/TUTORIAL.md)

> A 64-bit Raspberry Pi port of the usable `libuptech.so` interfaces from the old ARM32 runtime.

## Project scope

OpenLibUptech is an open compatibility implementation reconstructed from the original 32-bit image, Python wrapper, exported ABI, and live hardware protocols. The repository includes both auditable C source and a live-tested `libuptech.so` for Raspberry Pi AArch64, so users can run the prebuilt library or rebuild it.

This is not the vendor source and does not redistribute the original ARM32 image. The main function names and ctypes calling conventions are retained while the low-level implementation is provided as buildable C code.

## Verified features

| Module | Current implementation |
| --- | --- |
| ADC / digital IO / RGB | `/dev/spidev1.0`; 9 ADC channels, 8 IO channels, 2 RGB LEDs |
| MPU6500 | `/dev/i2c-1`, address `0x68`; 10 ms cache by default; accelerometer, gyroscope, and software complementary attitude |
| Chassis | `/dev/ttyAMA0`, GPIO4 half-duplex direction; brushed motors on IDs 7/8 |
| Fan | GPIO18 through pigpiod; CPU-temperature PWM (20%–100% at 800 Hz) with a safe temporary manual override |
| Acceptance panel | PyQt6 pages for environment, MPU, ADC, IO, RGB, fan, and motors |

## Quick start

On 64-bit Raspberry Pi OS:

```bash
sudo apt update
sudo apt install -y build-essential python3-pyqt6 python3-pigpio pigpio i2c-tools
file ./libuptech.so
python3 abi_smoke.py ./libuptech.so
python3 acceptance_gui.py ./libuptech.so
```

`file` should report `ELF 64-bit ... ARM aarch64`. See the [full tutorial](docs/TUTORIAL.md) for bus configuration, service installation, permissions, hardware acceptance, and rollback.

## Repository contents

- `libuptech.c` / `Makefile`: compatibility-library source and build entry point;
- `libuptech.so`: verified AArch64 prebuilt library for Linux ARM64;
- `acceptance_gui.py`: PyQt6 hardware acceptance panel;
- `abi_smoke.py`: ABI check without opening devices; `live_mpu_test.py` writes MPU initialization registers;
- `uptech_fan_control.py` / `uptech-fan.service`: CPU-temperature fan service and its GPIO18 Socket control interface;
- `pigpiod.service`: local pigpiod service;
- `99-uptech-ttyama0.rules`: udev permissions for the chassis UART;
- `hardware-acceptance-overview.png`: live acceptance screenshot;
- `requirements-gui.txt`: Python GUI dependencies.

## MPU6500 attitude and UART timing

`mpu6500_Get_Attitude()` returns `[pitch, roll, yaw]` in degrees. The attitude comes from a software complementary filter over the accelerometer and gyroscope; it is not a port of the vendor DMP. Yaw has no magnetometer reference and drifts. The cache samples every 10 ms by default; set `UPTECH_MPU_CACHE_MS` to another positive interval to change it. Setting it to `0` restores synchronous reads and makes `Get_Attitude` return `ENOTSUP`. Before the first frame it returns `EAGAIN`; placeholder zeroes are not valid attitude data.

The chassis UART no longer uses `tcdrain()`, whose fixed wait was about 8 ms. After a bounded `TIOCOUTQ` poll reports an empty software queue, TX direction stays enabled for a full frame's wire time plus 20 us (10 us per byte at 1 Mbps, 8N1). This covers bytes still in the hardware FIFO. Actual GPIO4/TX waveforms have not been archived; a successful send does not confirm physical motor stop. A complete send is retried at most three times, with a 1 ms frame gap after success. `Get_Accel` and `Get_Gyro` use the FSR captured with each sample. The user completed tilt and yaw observation tests; quantitative accuracy was not measured.

`make test` runs device-free fan tests and a Linux MPU/UART mock regression covering concurrent range changes, attitude axes and units, fork, and sampler shutdown. Hardware MPU and UART checks run on the Pi; the PTY protocol test still configures and toggles GPIO4 but never writes the real `/dev/ttyAMA0`. `_verify_mpu.py` changes the FSR temporarily and restores the initial range in `finally`.

Pi candidate verification on 2026-10-30 measured a 3.1 us median cached getter, 888.2 us synchronous reads, and about 1.03–1.05 g magnitude at rest. The candidate has the same 45 FUNC names as the old library. The first candidate was built in `~/openlib-work`; the motor-stop fix is in `~/openlib-stopfix-20261030`, and the user confirmed functional testing is complete. The checked-in `libuptech.so` now contains the same fix; `~/libuptech64` on the Pi still contains the old library. See the [validation record](docs/OPTIMIZATION-VALIDATION.md) and [comparison report](docs/REVERSE-ENGINEERING-REPORT.md).

## Current limits

- All 18 LCD/UGUI entry points remain ABI stubs: 17 return `ENOTSUP`, while `lcd_close` returns `0`;
- The MPU6500 internal temperature register is present, but no temperature ABI or GUI field is exported yet;
- Attitude is a software complementary-filter result, not DMP; yaw is relative and drifts;
- The tested chassis motors have no encoders, so position feedback is not treated as valid;
- Fan control is provided by pigpio services outside the `libuptech.so` ABI;
- 45 is the exported FUNC count, and the library is not fully stripped; the checked-in `.so` includes MPU attitude and the UART stop fix. Its SHA-256 and build details are in the validation record;
- No open-source license is declared yet; add a `LICENSE` file before formal redistribution.

![OpenLibUptech hardware acceptance GUI](./hardware-acceptance-overview.png)

## Contributions and issues

Include the Raspberry Pi OS version, `file libuptech.so` output, device nodes, and logs when reporting a problem. For motor, fan, or IO output work, disconnect the load first and keep the original 32-bit image available for rollback.
