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

这条 `make` 会覆盖当前目录的 `.so`，所以如果要保留预编译文件，请先复制一份到其他目录。Makefile 显式链接 `-lm`，软件姿态滤波不依赖 Python 宿主预先加载数学库；这不会改变导出 ABI。

默认的无设备测试只检查风扇控制：

```bash
make test
```

硬件或 PTY 验证使用显式命令，详见第 6 节；它们不属于默认 `make test`。

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
# 首次替换已有服务前保留一个可恢复副本
sudo cp -a /etc/systemd/system/uptech-fan.service \
  /etc/systemd/system/uptech-fan.service.bak.$(date +%Y%m%d-%H%M%S)
sudo install -m 0644 pigpiod.service /etc/systemd/system/pigpiod.service
sudo install -m 0755 uptech_fan_control.py /usr/local/bin/uptech_fan_control.py
sudo install -m 0644 uptech-fan.service /etc/systemd/system/uptech-fan.service
sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod.service uptech-fan.service
sudo systemctl restart uptech-fan.service
```

如果旧服务文件尚不存在，跳过第一条备份命令即可。新服务启动时先全速转 1 秒，再每 2 秒读取 Raspberry Pi CPU 温度：

| CPU 温度 | 自动占空比 |
| --- | --- |
| `<45°C` | 20% |
| `45–55°C` | 70% |
| `55–65°C` | 80% |
| `65–75°C` | 90% |
| `≥75°C` 或温度读取失败 | 100% |

每个分界点有 2°C 回差，避免温度在临界值附近时风扇反复变速。服务通过 `/run/openlibuptech/fan.sock` 独占 GPIO18；该 Socket 属于 `gpio` 组，模式为 `0660`。可以直接读回 PWM 和服务状态：

```bash
systemctl is-active pigpiod.service uptech-fan.service
pigs pfg 18
pigs prg 18
pigs gdc 18
python3 - <<'PY'
import json, socket
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.connect("/run/openlibuptech/fan.sock")
    s.sendall(b'{"command":"status"}\n')
    print(json.loads(s.recv(4096)))
PY
```

前三个值应为 `800`、`100` 和当前温度对应的占空比。Socket 响应会显示 `mode`、`temperature_c`、`duty` 和手动租约剩余时间。这个服务只控制风扇，不属于 `libuptech.so` 的 ABI。

## 6. 先跑只读检查

这些命令不会给 ADC/IO、风扇或执行器发送动作命令：

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

`live_mpu_test.py` 会写 MPU 初始化寄存器。`_verify_mpu.py` 会读取并切换 FSR 量程，并在 `try/finally` 中恢复原值；它读取软件姿态角，不是 DMP。

UART 回归可拆分运行。以下 PTY 协议测试不写真实 `/dev/ttyAMA0`，但仍会通过 `cds_servo_open()` 配置并翻转真实 GPIO4：

```bash
sudo systemctl stop uptech-fan.service
UPTECH_LIB=./libuptech.so python3 -m unittest tests.test_servo_protocol
sudo systemctl start uptech-fan.service
```

不要从这里的停服命令推断 PTY 测试会独占风扇；停服是为了把 GPIO/服务副作用显式隔离，初测完成后恢复。若只在未接硬件的兼容环境执行，无法证明 GPIO4 与真实总线的行为。

真实 UART 时序验证必须显式运行，并让两台电机保持零速；该命令会配置 GPIO4 并发送真实合法总线帧：

```bash
python3 _verify_servo_timing.py ./libuptech.so
```

`_verify_servo_timing.py` 测的是零速帧发送路径和 1 ms 帧间隔，不能替代真实非零电机控制或机械负载验收。仓库报告中的历史 3.1 µs / 890 µs 和 8 ms / 90 µs 数据来自旧轮实测；本轮修改尚未重测。

## 7. 启动验收界面

在树莓派本地桌面终端运行：

```bash
python3 acceptance_gui.py ./libuptech.so
```

界面没有安装成 systemd 服务，需要在有图形桌面会话的终端中启动。通过 SSH 启动时，必须自行设置与本地桌面一致的 `DISPLAY` / `WAYLAND_DISPLAY`；最简单的方式是在树莓派显示器上的终端运行。

界面默认只读。打开窗口不会自动启动风扇，也不会给底盘发送非零速度。点“运行只读验收”会打开底盘总线，从而配置 GPIO4；它读取 ADC/IO、MPU 和风扇状态，但不会发送非零电机速度。退出窗口或触发急停时，若底盘总线已打开，会发送 ID7/8 的合法零速帧。仅启动窗口、不点该套件且不打开电机会话时不发送电机命令。

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

风扇页默认是“自动温控”。点击“检测自动控制器”后会实时显示 CPU 温度、当前占空比和模式。输入期望占空比并勾选安全确认后，点击“手动锁定当前占空比”会立即切换为手动；按钮会变成“更新手动转速”，GUI 每秒续租，因此手动锁定不会被温控覆盖。点击“自动温控”会立即恢复温控曲线；关闭 GUI 或 3 秒未续租也会恢复自动。

“手动停止”会暂时保持 0%，但关闭 GUI 后也会恢复自动。正常部署时 GUI 只经 Socket 请求控制器，绝不会直接与服务竞争 GPIO18。仅在 Socket 完全不存在（服务没有安装）时，页面才会显示 pigpiod 手动兜底；这种兜底没有自动温控，测试完请将风扇设为 0%。

### 有刷底盘电机

这里的 `cds_servo_*` 是历史 ABI 名称，实际执行器是有刷电机。ID7 是左轮，ID8 是右轮；GUI 会处理右轮方向。测试前把车轮架空，使用短脉冲，完成后确认速度回到零。输入范围是 `0..1024`，协议实际有效幅值上限为 `1023`，很低的值可能不足以让电机启动。

底盘没有编码器，因此界面不会把位置读数当作有效反馈。

### MPU6500

MPU 页显示加速度、角速度、配置量程，以及 `Pitch`、`Roll`、`Yaw` 三个软件姿态角。成功后 GUI 显示数值，JSON 的 `mpu.attitude` 记录 `values_deg` 和相对 yaw 标记。等待首帧时显示“等待首帧”并记录 `EAGAIN`；设置 `UPTECH_MPU_CACHE_MS=0` 时显示“缓存关闭”并记录 `ENOTSUP`。这两种失败状态都不会把占位零值写入有效角度。

姿态是软件互补滤波，不是原厂 DMP；yaw 无磁力计绝对参考，会漂移。用户已完成倾角及 yaw 观察测试，未采集量化精度指标。

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

MPU 默认每 10 ms 缓存一次加速度和陀螺仪，并在软件中计算 `[pitch, roll, yaw]` 度。设 `UPTECH_MPU_CACHE_MS=0` 会关闭缓存：`Get_Accel` / `Get_Gyro` 改为同步 I²C 读取，`Get_Attitude` 返回 `ENOTSUP`。缓存开启但尚无首帧时返回 `EAGAIN`。

UART 发送使用有界 `TIOCOUTQ` 等待，不再调用 `tcdrain()`；队列清空后继续保持 GPIO4 发送态一整帧线上时间加 20 µs，9 字节速度帧至少再等 110 µs。一次失败会重试完整发送，最多 3 次，成功后保持 1 ms 帧间隔。`ReadReg` 校验 `FF FF`、ID、精确 `count + 6` 字节总数以及 `length == count + 2` 字段。用户已确认停机功能恢复正常并完成所列功能测试；真实 GPIO4/TX 波形未归档。

导出的 45 个函数中保留 18 个 LCD/UGUI 入口：17 个返回 `ENOTSUP`，`lcd_close` 返回 `0`。FUNC 数量是 45，不代表整个库完全 stripped；当前检出 `.so` 是旧产物，需由主流程在 Pi 上重建。

动态库生命周期：调用方必须先停止所有使用该库的 API 线程，再调用 `dlclose()`。库会先停止并 join 采样线程，再关闭设备 fd。`pthread_atfork` 在 fork 前锁住库内互斥量，父子进程分别释放；子进程清空旧缓存，调用 `mpu6500_open()` 后可重建采样线程。Pi 模拟 I²C 回归已验证 fork 时另一线程持有 I²C 锁的场景；这不保证父子同时操作同一真实总线的协议安全，应用推荐 fork 后立即 exec。

### 本轮最小回归

在 Linux/Pi 项目目录执行 `make test`，仅运行无设备风扇、MPU/UART 和可选 PyQt6 模拟测试。模拟测试覆盖缓存比例、四个并发调用线程、姿态轴向/度数、fork 锁继承、采样线程退出、FIFO 未空时的发送方向保持，以及两轮短脉冲归零和逐轮失败提示；未安装 PyQt6 时跳过 GUI 测试。真实串口协议测试需显式运行：

```sh
UPTECH_LIB=./libuptech.so python3 -m unittest discover -s tests -p test_servo_protocol.py -v
python3 _verify_mpu.py ./libuptech.so
UPTECH_MPU_CACHE_MS=0 python3 _verify_mpu.py ./libuptech.so
```

PTY 测试仍会翻转 GPIO4；MPU 脚本会切换量程并恢复初始值。这些命令不向真实电机 UART 发帧。2026-10-30 本轮结果、候选哈希与未完成的硬件项目见 `OPTIMIZATION-VALIDATION.md`。

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

### 风扇没有自动调速、转速不对或频繁变化

确认 `pigpiod.service` 和 `uptech-fan.service` 都是 active，然后检查服务日志、CPU 温度和 Socket 状态：

```bash
systemctl status uptech-fan.service --no-pager
journalctl -u uptech-fan.service -n 50 --no-pager
cat /sys/class/thermal/thermal_zone0/temp
```

不要运行其他会直接写 GPIO18 的脚本；自动服务应是唯一写入者。若要临时恢复旧固定 80% 服务，先恢复安装前备份的 service 文件、移除 `/usr/local/bin/uptech_fan_control.py`，再 `daemon-reload` 和重启服务。

### 温度、LCD 或姿态角在哪里

MPU6500 有芯片内部温度寄存器，但当前兼容 ABI 和 GUI 没有温度函数。当前没有确认独立的外部温度传感器。LCD/UGUI 的 18 个入口保留为 ABI 桩：17 个返回 `ENOTSUP`，`lcd_close` 返回 `0`。软件姿态角可用于观察姿态变化，不是 DMP，yaw 会漂移；物理观察测试已由用户完成，尚无量化精度指标。

## 11. 回滚

保留旧镜像时，回滚只需要关机并换回旧 SD 卡。若只回滚服务文件，可以恢复你在安装前保存的 `/etc/systemd/system/uptech-fan.service`，然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart uptech-fan.service
```

不要删除旧镜像，直到 ADC、IO、RGB、MPU、风扇和电机都完成实际验收。
