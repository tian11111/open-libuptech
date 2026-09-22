# OpenLibUptech

[中文](README.md) · [English](README.en.md) · [完整教程](docs/TUTORIAL.md)

> 把旧版 ARM32 `libuptech.so` 的可用接口带到 64 位 Raspberry Pi OS。

## 项目定位

OpenLibUptech 是根据原有 32 位系统镜像、Python 封装、ABI 和实机协议复刻的开源兼容实现。仓库同时提供 C 源码和一份已经在 Raspberry Pi AArch64 上验证过的 `libuptech.so`，方便直接使用或自行重编译。

它不是原厂源码，也不重新发布原厂 ARM32 镜像。原版函数名和主要 ctypes 调用约定被保留，底层实现改为可检查、可编译的 C 代码。

## 已验证功能

| 模块 | 当前实现 |
| --- | --- |
| ADC / 数字 IO / RGB | `/dev/spidev1.0`；9 路 ADC、8 路 IO、2 颗 RGB LED |
| MPU6500 | `/dev/i2c-1`、地址 `0x68`；加速度和陀螺仪原始数据 |
| 底盘 | `/dev/ttyAMA0`、GPIO4 半双工；ID7/ID8 有刷电机 |
| 风扇 | GPIO18、pigpiod；开机 800 Hz / 80% PWM |
| 验收界面 | PyQt6：环境、MPU、ADC、IO、RGB、风扇、电机页面 |

## 快速开始

在 64 位 Raspberry Pi OS 上：

```bash
sudo apt update
sudo apt install -y build-essential python3-pyqt6 python3-pigpio pigpio i2c-tools
file ./libuptech.so
python3 abi_smoke.py ./libuptech.so
python3 acceptance_gui.py ./libuptech.so
```

`file` 应报告 `ELF 64-bit ... ARM aarch64`。完整的总线配置、服务安装、权限、验收步骤和回滚方式见[完整教程](docs/TUTORIAL.md)。

## 仓库内容

- `README.en.md`：English entry page；
- `libuptech.c` / `Makefile`：兼容库源码和构建入口；
- `libuptech.so`：已验证的 AArch64 预编译库，仅适用于 Linux ARM64；
- `acceptance_gui.py`：硬件验收界面；
- `abi_smoke.py` / `live_mpu_test.py`：ABI 和 MPU 只读验证脚本；
- `pigpiod.service` / `uptech-fan.service`：本机 pigpiod 和开机 80% 风扇服务；
- `99-uptech-ttyama0.rules`：底盘 UART 的 udev 权限规则；
- `hardware-acceptance-overview.png`：实机验收截图；
- `requirements-gui.txt`：Python GUI 依赖。

## 当前边界

- LCD/UGUI 符号只保留 ABI 桩，返回 `ENOTSUP`；
- MPU6500 芯片内部温度寄存器已确认存在，但当前 ABI 和 GUI 尚未导出温度接口；
- DMP 姿态角返回 `ENOTSUP`；
- 底盘电机没有编码器，位置读取不作为有效反馈；
- 风扇控制属于 pigpio 服务，不属于 `libuptech.so` ABI；
- 仓库当前还没有声明开源许可证，正式对外发布前请补充 `LICENSE`。

![OpenLibUptech hardware acceptance GUI](./hardware-acceptance-overview.png)

## 贡献与问题反馈

请提交能复现问题的 Raspberry Pi OS 版本、`file libuptech.so` 输出、设备节点和日志。涉及电机、风扇或 IO 输出时，先断开负载并保留原 32 位镜像作为回滚。
