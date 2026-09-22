# OpenLibUptech 使用教程

[返回中文 README](../README.md) · [English README](../README.en.md)

这份教程面向手里有旧版 UpTech/TechStar 32 位镜像、现在要迁移到 64 位 Raspberry Pi OS 的用户。目标是先保留回滚路径，再确认总线和设备节点，最后启动只读验收。

## 1. 你会得到什么

仓库提供：

- `libuptech.c` 和 `Makefile`：可以审计和重新编译的兼容实现；
- `libuptech.so`：已经在 Raspberry Pi AArch64 上验证过的预编译库；
- `acceptance_gui.py`：PyQt6 硬件验收界面；
- `abi_smoke.py` 和 `live_mpu_test.py`：不改硬件输出的快速检查脚本；
- `pigpiod.service`、`uptech-fan.service` 和 udev 规则：树莓派部署文件。

本项目不是原厂 ARM32 二进制的重新发布，也不声称拥有原厂源码。它是根据原镜像行为、导出符号和实际协议写出的兼容层。

## 2. 先保留旧系统

在替换 32 位系统前，至少保留一张可以启动的旧 SD 卡或完整镜像。记录旧系统中的：

```bash
python3 -V
uname -m
ldd --version | head -n 1
```

不要把旧版 ARM32 `libuptech.so` 直接复制到 64 位 Python 环境中。先用本仓库的 AArch64 版本验证硬件，出现问题时可以切回旧卡进行对比。

## 3. 安装代码和依赖

在树莓派上克隆仓库：

```bash
git clone https://github.com/tian11111/open-libuptech.git
cd open-libuptech
```

如果当前默认分支没有包含最新验收界面，切换到开发分支：

```bash
git switch test/pyqt-acceptance
```

安装依赖：

```bash
sudo apt update
sudo apt install -y build-essential python3-pyqt6 python3-pigpio pigpio i2c-tools
```

仓库自带预编译库时可以直接检查：

```bash
file ./libuptech.so
sha256sum ./libuptech.so
```

必须看到 `ELF 64-bit` 和 `ARM aarch64`。如果需要从源码重编译：

```bash
make clean
make
file ./libuptech.so
```

这条 `make` 会覆盖当前目录的 `.so`，所以如果要保留预编译文件，请先复制一份到其他目录。

## 4. 配置 Raspberry Pi 总线

编辑 `/boot/firmware/config.txt`，确认至少有：

```ini
dtparam=i2c_arm=on
dtparam=spi=on

[all]
dtoverlay=spi1-1cs,cs0_pin=16
enable_uart=1
dtoverlay=disable-bt
```

重启后应出现这些节点：

```bash
ls -l /dev/i2c-1 /dev/spidev1.0 /dev/ttyAMA0
```

两个容易造成冲突的配置不要同时启用：

- `dtoverlay=w1-gpio` 可能占用 GPIO4，而 GPIO4 是底盘总线方向控制；
- `spi1-3cs` 可能占用 GPIO18，而 GPIO18 用于风扇 PWM。

底盘 UART 不能同时被 Linux 当作登录串口：

```bash
# 从 /boot/firmware/cmdline.txt 删除 console=serial0,115200
sudo systemctl disable --now serial-getty@ttyAMA0.service
sudo install -m 0644 99-uptech-ttyama0.rules /etc/udev/rules.d/99-uptech-ttyama0.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=ttyAMA0
```

## 5. 设置权限并安装服务

把当前用户加入常见硬件访问组，然后重新登录：

```bash
sudo usermod -aG dialout,spi,i2c,gpio "$USER"
```

如果某个组在系统中不存在，把它从命令中删掉即可；`99-uptech-ttyama0.rules` 只负责 `ttyAMA0` 的 `dialout` 权限。

安装本机 pigpiod 和风扇服务：

```bash
sudo install -m 0644 pigpiod.service /etc/systemd/system/pigpiod.service
sudo install -m 0644 uptech-fan.service /etc/systemd/system/uptech-fan.service
sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod.service uptech-fan.service
sudo systemctl restart uptech-fan.service
```

风扇服务启动后会把 GPIO18 设置为 800 Hz、PWM 范围 100、占空比 80。可以直接读回：

```bash
systemctl is-active pigpiod.service uptech-fan.service
pigs pfg 18
pigs prg 18
pigs gdc 18
```

最后三个值应为 `800`、`100`、`80`。这个服务只控制风扇，不属于 `libuptech.so` 的 ABI。

## 6. 先跑只读检查

这些命令不会给 ADC/IO、风扇或电机发送动作命令：

```bash
file ./libuptech.so
python3 abi_smoke.py ./libuptech.so
/usr/sbin/i2cdetect -y 1
/usr/sbin/i2ctransfer -y 1 w1@0x68 0x75 r1
```

MPU6500 兼容器件通常应在 `0x68` 出现，WHO_AM_I 常见返回 `0x70`。SPI、UART 和 GPIO 节点则用 `ls -l` 确认。

需要验证加速度和陀螺仪时再运行：

```bash
python3 live_mpu_test.py ./libuptech.so
```

脚本最后会报告姿态角接口 `ENOTSUP`，这是当前已知边界，不代表加速度和陀螺仪读取失败。

## 7. 启动验收界面

在树莓派本地桌面终端运行：

```bash
python3 acceptance_gui.py ./libuptech.so
```

界面没有安装成 systemd 服务，需要在有图形桌面会话的终端中启动。通过 SSH 启动时，必须自行设置与本地桌面一致的 `DISPLAY` / `WAYLAND_DISPLAY`；最简单的方式是在树莓派显示器上的终端运行。

界面默认只读。打开窗口不会自动启动风扇，也不会给底盘发送非零速度。

## 8. 各页面怎么验收

### ADC

点击“打开 SPI”，再点击“读取一次”或开启“5 Hz 实时刷新”。板上有 9 路外部 ADC：`ADC0` 到 `ADC8`。协议返回数组的第 10 个 `uint16_t` 是板载电源电压原始值，不是 `ADC9`。

### 数字 IO

IO 页有 8 路：`IO0` 到 `IO7`。可以单次读取，也可以在本页独立开启“5 Hz 实时刷新”。输入电平和模式读取不需要解锁；写模式或写电平前要勾选并确认写操作。

### RGB LED

RGB 页有索引 `0` 和 `1` 两颗 LED：

1. 在本页勾选解锁并确认；
2. 点击颜色按钮打开选择器；
3. 选择颜色后点击“写 RGB 颜色”；
4. 需要关闭时点击“关闭全部 RGB”。

颜色按 `0xRRGGBB` 表示，例如 `0xFF0000` 是红色，`0x00FF00` 是绿色，`0x0000FF` 是蓝色。

### 风扇

风扇页默认输入值是 80%。勾选安全确认后点击“连接本机 pigpiod”，再点击“应用 PWM”。“停止风扇”会把 GPIO18 拉低。开机默认的 80% 则由 `uptech-fan.service` 设置。

### 有刷底盘电机

这里的 `cds_servo_*` 是历史 ABI 名称，实际执行器是有刷电机。ID7 是左轮，ID8 是右轮；GUI 会处理右轮方向。测试前把车轮架空，使用短脉冲，完成后确认速度回到零。输入范围是 `0..1024`，协议实际有效幅值上限为 `1023`，很低的值可能不足以让电机启动。

底盘没有编码器，因此界面不会把位置读数当作有效反馈。

## 9. 硬件节点和协议摘要

| 功能 | 节点 / 参数 |
| --- | --- |
| ADC / IO / RGB | `/dev/spidev1.0`，SPI mode 0，1 MHz，GPIO16 CS |
| MPU6500 | `/dev/i2c-1`，默认地址 `0x68` |
| 底盘总线 | `/dev/ttyAMA0`，1 Mbps，GPIO4 半双工方向 |
| 风扇 | GPIO18，pigpiod，800 Hz PWM |

`ADC_GetAll` 返回 10 个小端 `uint16_t`；IO、RGB 和底盘协议已经在 `libuptech.c` 中实现。Python 用户可以继续通过 ctypes 加载库：

```python
import ctypes

lib = ctypes.CDLL("./libuptech.so", use_errno=True)
lib.mpu6500_open.restype = ctypes.c_int
print("MPU open:", lib.mpu6500_open())
```

## 10. 故障排查

### `wrong ELF class` 或无法加载 `.so`

执行 `file libuptech.so`。本项目的预编译库是 AArch64，不能在 32 位 ARM 或 x86 Windows Python 中加载；源码可以在目标架构上重新编译。

### 找不到 SPI / I²C / UART 节点

检查 `config.txt`、设备树 overlay、串口登录服务和 GPIO 冲突。修改后重启，再运行：

```bash
ls -l /dev/i2c-1 /dev/spidev1.0 /dev/ttyAMA0
systemctl status serial-getty@ttyAMA0.service --no-pager
```

### GUI 能打开但读不到硬件

先运行 `abi_smoke.py`，再检查用户组和 `ls -l /dev/...` 的权限。ADC 和 IO 共用 SPI 设备，确认没有其他程序长期占用 `/dev/spidev1.0`。

### 风扇没有 80% 或频繁变化

确认 `pigpiod.service` 和 `uptech-fan.service` 都是 active，然后重启风扇服务并读回 `pfg/prg/gdc`。不要同时运行多个会写 GPIO18 的风扇脚本。

### 温度、LCD 或姿态角在哪里

MPU6500 有芯片内部温度寄存器，但当前兼容 ABI 和 GUI 没有温度函数。当前没有确认独立的外部温度传感器。LCD/UGUI 只保留符号桩；DMP 姿态角返回 `ENOTSUP`。

## 11. 回滚

保留旧镜像时，回滚只需要关机并换回旧 SD 卡。若只回滚服务文件，可以恢复你在安装前保存的 `/etc/systemd/system/uptech-fan.service`，然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart uptech-fan.service
```

不要删除旧镜像，直到 ADC、IO、RGB、MPU、风扇和电机都完成实际验收。
