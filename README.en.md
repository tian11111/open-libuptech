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
| MPU6500 | `/dev/i2c-1`, address `0x68`; raw accelerometer and gyroscope data |
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
- `abi_smoke.py` / `live_mpu_test.py`: ABI and read-only MPU checks;
- `uptech_fan_control.py` / `uptech-fan.service`: CPU-temperature fan service and its GPIO18 Socket control interface;
- `pigpiod.service`: local pigpiod service;
- `99-uptech-ttyama0.rules`: udev permissions for the chassis UART;
- `hardware-acceptance-overview.png`: live acceptance screenshot;
- `requirements-gui.txt`: Python GUI dependencies.

## Current limits

- LCD/UGUI symbols are ABI stubs returning `ENOTSUP`;
- The MPU6500 internal temperature register is present, but no temperature ABI or GUI field is exported yet;
- DMP attitude output returns `ENOTSUP`;
- The tested chassis motors have no encoders, so position feedback is not treated as valid;
- Fan control is provided by pigpio services outside the `libuptech.so` ABI;
- No open-source license is declared yet; add a `LICENSE` file before formal redistribution.

![OpenLibUptech hardware acceptance GUI](./hardware-acceptance-overview.png)

## Contributions and issues

Include the Raspberry Pi OS version, `file libuptech.so` output, device nodes, and logs when reporting a problem. For motor, fan, or IO output work, disconnect the load first and keep the original 32-bit image available for rollback.
