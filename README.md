# libuptech64 compatibility library

This is a clean-room aarch64 compatibility implementation based on the public
Python wrapper ABI and observable protocol constants in the shipped ARM32 ELF.

Implemented hardware paths:

- ADC/8-bit IO/LED controller over `/dev/spidev1.0`, mode 0, 1 MHz. The
  single chip-select is remapped to GPIO16, the board's physical SPI1 CE2 pin.
- CDS servo bus over `/dev/ttyAMA0`, 1 Mbps, with GPIO4 half-duplex direction.
- MPU6500 raw accelerometer and gyroscope access over `/dev/i2c-1`, address 0x68.

LCD drawing and MPU DMP attitude functions are exported for ABI compatibility
but return `-1` with `errno=ENOTSUP`.

Environment overrides:

- `UPTECH_SPI_DEVICE`
- `UPTECH_UART_DEVICE`
- `UPTECH_GPIOCHIP`
- `UPTECH_DIRECTION_GPIO`
- `UPTECH_I2C_DEVICE`
- `UPTECH_MPU_ADDRESS`

Required Raspberry Pi boot configuration:

```ini
dtparam=i2c_arm=on
dtparam=spi=on

[all]
dtoverlay=spi1-1cs,cs0_pin=16
enable_uart=1
dtoverlay=disable-bt
```

Do not enable the default `dtoverlay=w1-gpio` at the same time. It claims
GPIO4, which this library needs for CDS half-duplex direction control.
Do not use the default `spi1-3cs` overlay with the fan: it claims GPIO18 as
SPI1 CS0, while the legacy Python driver uses GPIO18 for hardware PWM.

The legacy MBri `uptech.py` also requires the Python `pigpio` client and a
running `pigpiod`. The accompanying `pigpiod.service` runs the daemon in the
foreground under systemd and restricts its socket to localhost. Its fan setup
uses GPIO18 hardware PWM at 20 kHz and full duty cycle.

To make the fan start after every boot, install both systemd units. The fan unit
starts only after pigpiod is ready and stops PWM before pigpiod shuts down:

```bash
sudo install -m 0644 pigpiod.service /etc/systemd/system/pigpiod.service
sudo install -m 0644 uptech-fan.service /etc/systemd/system/uptech-fan.service
sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod.service uptech-fan.service
```

Verified on 2026-09-21 on a Raspberry Pi 4 running aarch64/Python 3.13:

- `pyuptech==0.1.6.5` loaded this library and read live MPU6500 data.
- ADC/IO opened and returned ten ADC channels plus the eight-bit input mask.
- The CDS UART and GPIO4 direction line opened and closed successfully; no
  servo movement command was sent.

Build on the Raspberry Pi with `make`. Keep the original vendor library as a
rollback copy and test with motors lifted from the ground.

## PyQt hardware acceptance panel

`acceptance_gui.py` is a Raspberry Pi-side acceptance panel for the AArch64
library. It loads the shared library directly with `ctypes`, so opening the GUI
does **not** reproduce the legacy wrapper's automatic full-speed fan startup.

Install the GUI dependencies and run it from a Raspberry Pi desktop session:

```bash
sudo apt update
sudo apt install python3-pyqt6 python3-pigpio
python3 acceptance_gui.py ./libuptech.so
```

The read-only suite checks the ABI, device nodes, MPU6500 samples, ADC/IO input,
CDS-bus controller feedback for IDs 7 and 8, and the local pigpiod connection.
It does not start the fan or send nonzero motor speed. ADC/IO writes, fan PWM,
and brushed chassis motor commands each have an explicit safety unlock. Motor
tests are limited to `-100..100`, use a `100..1000 ms` pulse, automatically send
zero speed, and always expose an emergency-stop button.

The `cds_servo_*` prefix is retained only because it is part of the vendor ABI.
In the acceptance panel IDs 7 and 8 are described as the brushed chassis motor
controller, not as physical servos.
