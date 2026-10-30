# 原厂 libuptech.so 与 OpenLibUptech 复刻版逆向对比报告

日期：2026-10-29

最新修订：2026-10-30 用户报告右轮脉冲结束后不停转。已修复 `TIOCOUTQ` 清空后过早关闭 GPIO4 的可复现时序缺陷，并改进逐轮停机失败提示；新候选在 `~/openlib-stopfix-20261030`，SHA-256 `e495984693391a121451cfa45d8769f78c67343cd0894f2e8eb645eec19c5c10`。后续用户确认“现在正常了”，并确认所列重复脉冲、不同停机入口、传感器同时刷新和 MPU 功能检查均已测过，本轮功能验收按用户反馈收尾，无需重复测试。具体次数、角度误差及漂移率未采集，不补造量化结论。旧候选及其性能记录保留用于追溯。现场 TX/GPIO4 波形未归档，属于可选的原因验证，不作为功能验收阻塞项，也不能据此宣称唯一根因已在现场证实。详见产品仓库 `docs/OPTIMIZATION-VALIDATION.md` 的右轮反馈节。

仓库预编译库同步（2026-10-30）：`libuptech.so` 已同步为上述 `e4959846…c5c10` 修复版。只读核对树莓派当前 GUI（PID 2515）映射路径与文件 inode，确认加载的是 `~/openlib-stopfix-20261030/libuptech.so`；正式路径 `~/libuptech64/libuptech.so` 仍为旧版。本次未替换正式目录。

## 0. 结论摘要

复刻版在**总线协议层面与原厂高度一致**（ADC 字节序、IO/RGB 帧、底盘 SCS 帧格式全部对得上），
不是"猜"出来的实现。但在**架构层面有三处实质差距**，且差距方向都不是"复刻引入的额外开销"，
而是原厂采用了复刻版没有采用的机制：

1. **原厂 MPU 数据是异步缓存的，复刻版是同步阻塞读** —— 单次读取差约 400 倍；
2. **原厂底盘发送不用 `tcdrain`，复刻版用了** —— 单帧差约 8 倍；
3. **原厂有完整 DMP + UGUI/LCD，复刻版是桩或缺失** —— 功能缺失 219 个导出符号。

上列是 2026-10-29 逆向基线结论；本文件后续当前阶段修订记录了 2026-10-30 候选状态和验证。

> **2026-10-30 初版候选修订（历史记录；后续更新见文首和验收记录）**：以下第 3–4、6–7、9 节保留逆向当时的静态对比和旧轮实测证据。
> 当前源码已在工作树中改为 MPU 默认 10 ms 缓存并用软件互补滤波输出姿态角，UART 发送改为有界
> `TIOCOUTQ` 等待、最多 3 次完整重试并保留成功后的 1 ms 帧间隔；18 个 LCD/UGUI 入口中 17 个返回
> `ENOTSUP`、`lcd_close` 返回 0。当前源码已在 Pi 重新构建，并完成 MPU、PTY、GUI 和 ABI 检查；
> 候选构建 SHA-256 为 `47844441d1ddfe5e80ca5ce277655edec6ad731597e39627c20c35359989ee32`。
> 仓库 `libuptech.so` 仍是旧产物（SHA-256 `489b28caa1aa9f4ae363d0cb7878a215c33a6c648e18c4b4b869c891c3b6bbe5`），
> 本轮重新测得缓存 getter 中位 3.1 µs、同步 888.2 µs；未重测裸 tty 的历史 8 ms / 90 µs 数据。
> 物理倾斜精度和真实电机负载仍未验收。
> 下文“复刻版”性能描述均特指历史构建，不能覆盖当前源码候选。

---

## 1. 证据基线

| 项 | 原厂 | 复刻版 |
|---|---|---|
| 文件 | `pyuptech/lib/libuptech.so` | `open-libuptech/libuptech.so` |
| SHA-256 | `75dfa819e8e43d786453150c9536d14747a5594686fdccf5a64278ca597ddf22` | `489b28caa1aa9f4ae363d0cb7878a215c33a6c648e18c4b4b869c891c3b6bbe5` |
| 大小 | 219,512 B | 73,040 B |
| 格式 | `ELF 32-bit LSB, ARM, EABI5` (ARM32) | `ELF 64-bit LSB, AArch64` |
| 符号 | **未 strip**，264 导出 FUNC + 59 OBJECT | 保留节表和符号表，45 导出 FUNC + 0 OBJECT |
| `.text` | 47,180 B（`0x55e8`–`0x10e4c`） | ELF 节表存在 |
| 依赖 | `libpigpiod_if2.so.1`, `librt`, `libm`, `libwiringPi`, `libpthread`, `libc` | 旧产物为 `libc` 等；新候选显式依赖 `libm` / `libc` |
| 内部函数（有 size） | 330 个，46,716 B | — |

原厂 `e_flags=0x5000400`（EF_ARM_EABI_VER5 | hard-float ABI）。

**分析方法**：`objdump -d` 全量反汇编 + 手工语义还原；ELF 结构、符号表、重定位、GNU_HASH
由自写 Python 解析器直接读取（`_re/` 目录）。REA（reverse-engineer-anything）因 Windows 端
Ghidra provider 只接受 PE 格式、且 ELF 布局分析需 Linux，本次仅作能力边界确认，未产出伪代码。

---

## 2. 架构差异总览

### 2.1 原厂经 pigpio 守护进程，复刻版直连内核设备节点

原厂全部外设访问经 `libpigpiod_if2` → `pigpiod` 守护进程：

```
adc_io_open:   pigpio_start(0,0) → spi_open(pi, 2, 1000000, 256)
mpu6500_open:  pigpio_start(0,0) → i2c_open(pi, 1, 0x68, 0) → set_mode(pi,18,1) → gpio_write(pi,18,1)
cds_servo_open: pigpio_start(0,0) → set_mode(pi,4,1) → gpio_write(pi,4,0) → drv_serial_open("/dev/ttyAMA0")
SERVO_BUS_TX_ENABLE/DISABLE: gpio_write(pi, 4, 1/0)
```

复刻版直接 `open("/dev/spidev1.0")` / `ioctl(I2C_SLAVE)` / `open("/dev/gpiochip4")`。

**影响**：这一层原厂多一次 Unix socket 往返，理论延迟更高；但复刻版在 SPI/I²C 单次调用上
实测与裸系统调用持平（见 §5），所以本项**不构成复刻版的劣势**，反而复刻版更直接。

### 2.2 原厂 MPU 走 DMP + SIGALRM 异步刷新

这是最关键的架构差异，详见 §3。

---

## 3. MPU6500：同步阻塞 vs 异步缓存

### 3.1 原厂 `mpu6500_Get_Accel`（`0xc080`，92 B）

反汇编等价 C：

```c
int mpu6500_Get_Accel(float *out) {
    float sens = *(uint16_t *)(&accel_sens + 20);   // 0x5f674+20
    out[0] = (float)mpu_accel[0] / sens;
    out[1] = (float)mpu_accel[1] / sens;
    out[2] = (float)mpu_accel[2] / sens;
    return 0;   // 注意：无返回值判断，无 I2C 事务
}
```

**函数体里没有任何 I2C 读写**，只有 3 次除法（`vdiv.f32`）。`mpu6500_Get_Gyro`（`0xc0dc`）同理，
`mpu6500_Get_Attitude`（`0xc134`，52 B）更简单，只是把全局 `Pitch` / `Roll` / `Yaw` 三个 float 拷到输出。

### 3.2 数据由谁填充：`mpu6500_dmp_update` + `signal_handler`

```asm
; mpu6500_dmp_init 尾部 (0xc01c..0xc02c)
bl  signal@plt          ; signal(SIGALRM=14, signal_handler)
mov r0, #100
bl  set_timer@plt       ; set_timer(100)

; signal_handler (0xbea4, 4 B)
bea4: b  mpu6500_dmp_update@plt    ; 尾调用
```

`set_timer`（`0xb8c4`，80 B）的整数运算：`r0 = 1000/100 = 10`，再
`r3 = (r0 + ((r0*31)<<2)) << 3` = `(10 + 1240) << 3` = **10000 µs**，
写入 `it_interval.tv_usec` 与 `it_value.tv_usec`。

→ **定时器周期 10 ms = 100 Hz**（与 `dmp_set_fifo_rate(100)` 一致）。

`mpu6500_dmp_update`（`0xbcf8`，428 B）在信号处理上下文里：

1. `dmp_read_fifo(mpu_gyro, mpu_accel, mpu_quat, &ts, &sensors, &more)` 读硬件 FIFO；
2. 把 quat 的 s16 值乘 `2^-30`（常量 `0x30800000` = 9.3132e-10，即 q30 定标）换算成 float，写回 `mpu_quat`；
3. 用 `asin` + `atan2` 由四元数算出欧拉角；
4. 乘 `57.3`（`0xbe68` 起的 double `d8`，是 `180/π` 的近似值）转角度制；
5. 写入全局 `Pitch`（`0x5f748`）、`Roll`（`0x5f740`）、`Yaw`（`0x5f728`）。

`mpu6500_dmp_init`（`0xbea8`，472 B）的初始化序列（全部为常量，可作为配置基线）：

| 步骤 | 参数 |
|---|---|
| WHO_AM_I 校验 | 读 `0x75`，必须 == `0x70`（否则返回 -1） |
| 复位 | `PWR_MGMT_1(0x6b) = 1`，`usleep_s(100000)`=100 ms |
| `mpu_init(0)` | |
| `mpu_set_sensors(0x78)` | accel + gyro + temp |
| `mpu_set_gyro_fsr(2000)` | ±2000 dps |
| `mpu_set_accel_fsr(8)` | ±8 g |
| `mpu_set_lpf(188)` | 188 Hz |
| `mpu_configure_fifo(0x78)` | |
| `mpu_set_sample_rate(1000)` | 1 kHz |
| `dmp_load_motion_driver_firmware()` | |
| `dmp_set_orientation(inv_orientation_matrix_to_scalar(&matrix))` | |
| `dmp_enable_feature(0x173)` | |
| `dmp_set_fifo_rate(100)` | |
| `mpu_set_dmp_state(1)` | |
| 预热循环 | `usleep_s(10000)` 直到 `dmp_read_fifo` 成功 |
| `run_self_test()`、`mpu_get_gyro_sens`、`mpu_get_accel_sens` | 填充灵敏度 |
| `signal(14,h)` + `set_timer(100)` | **启动 100 Hz 异步刷新** |

### 3.3 复刻版对应实现

逆向当时，复刻版 `mpu6500_Get_Accel` / `Get_Gyro` **每次调用都做实时 I²C 事务**（写寄存器指针 +
读 6 字节），`mpu6500_Get_Attitude` 直接返回 `ENOTSUP`，且 `mpu6500_dmp_init` 只做 5 次寄存器写入，
不加载 DMP 固件。

当前源码修订（2026-10-30）默认以 10 ms 周期在后台采样，getter 返回最后快照；
`UPTECH_MPU_CACHE_MS=0` 恢复同步读取，且 `Get_Attitude` 返回 `ENOTSUP`。姿态不是 DMP 移植，
而是软件互补滤波，输出 `[pitch, roll, yaw]` 度；首帧前返回 `EAGAIN`，yaw 是无磁力计基准的相对角，
会漂移。FSR 从寄存器读回后选择灵敏度，采样与比例读取使用同一 I²C 临界区，并随缓存样本一起发布。
本轮候选已经实测，软件公式与芯片轴向保持一致：pitch 绕 Y，roll 绕 X，yaw 绕 Z；车体安装方向与物理精度仍需验收。

### 3.4 量化差距（历史轮实机实测）

| 接口 | 原厂（分析推断） | 复刻版（实测） | 倍数 |
|---|---|---|---|
| `Get_Accel` | ≈ 1–2 µs（读缓存 + 3 除法） | 890 µs | ~500× |
| `Get_Gyro` | ≈ 1–2 µs | 890 µs | ~500× |
| `Get_Attitude` | 有效欧拉角 | `ENOTSUP` | 功能缺失 |

历史构建的 890 µs 已实测为 I²C 总线本身耗时（裸 `write+read` 为 881.9 µs），**库封装无额外开销**。
2026-10-30 候选复测：`Get_Accel` / `Get_Gyro` 缓存中位数均为 3.1 µs，同步模式均为 888.2 µs，
约为同步模式的 1/286。缓存数据每约 10 ms 更新，调用速度不代表新样本频率，也不是与原厂的运行时速度比。

---

## 4. 底盘串口：`tcdrain` 的 8 ms 固定开销

### 4.1 原厂 `cds_servo_SendFrame`（`0x57b0`，304 B）

完整动作序列（`0x5844`–`0x58c4`）：

```c
retry:                                    // 最多 3 次
  SERVO_BUS_TX_ENABLE();                  // gpio_write(pi, 4, 1)
  drv_serial_read(fd, buf, 255);          // 清空残留
  drv_serial_write(fd, frame, framelen);
  usleep(50);                             // 50 µs
  SERVO_BUS_TX_DISABLE();                 // gpio_write(pi, 4, 0)
  usleep(1000);                           // 1000 µs
  n = drv_serial_read(fd, buf, 255);
  tries++;
  if (n > 5 || tries >= 3) break;
  goto retry;
```

帧构造（`0x57c4`–`0x5840`）：`[0xFF, 0xFF, id, len, inst, params..., ~checksum]`，
checksum = `(id + len + inst + Σparams) & 0xFF` 取反。

**关键点：全库无 `tcdrain`**。`nm -D` 中不存在该符号；`objdump` 中调用次数为 0。
原厂只靠 `usleep(50)` + 内核 FIFO 把 8 字节推出去。

### 4.2 复刻版

逆向当时的复刻版 `servo_send_locked` 在 `write_all()` 之后调用 `tcdrain(fd)`。当前源码已移除该调用：
写入后用 `TIOCOUTQ` 轮询排空，超时预算为 1 ms 加帧长乘 10，成功后仍有 1 ms 间隔；
`TIOCOUTQ` 不支持时回退到帧长对应的 1 Mbps 传输时间。方向切换、写入、排空或切回失败都会重试
整帧，最多 3 次。现有测试用 PTY 隔离真实 UART：它仍会配置并翻转 GPIO4，但不写真实
`/dev/ttyAMA0`；真实合法帧、非零电机和底盘控制周期仍未完成本轮硬件验收。

实测（树莓派 5，1 Mbps，帧长 8 B）：

| 实现 | 每次耗时 | 说明 |
|---|---|---|
| 裸 `write` only | 2.0 µs | 仅入 FIFO |
| 裸 `write + tcdrain` | **8107 µs** | 与帧长无关 |
| 裸 `write + TIOCOUTQ 轮询` | 98.5 µs | 驱动队列报告清空；硬件最后字节时序未用示波器验证 |
| 裸 `write + usleep(80)` | 140.6 µs | 近似原厂做法 |
| 历史复刻版 `cds_servo_SendFrame` | **8040 µs** | 受 `tcdrain` 支配 |

`tcdrain` 在 8/16/32/64 字节下耗时恒为 ≈8 ms，说明它踩到的是 Linux tty 层固定轮询周期，
而不是在等数据发完（1 Mbps 下 8 字节理论仅 80 µs）。

**历史量化差距**：原厂 ≈1.05 ms（50 + 1000 µs + 传输），当时复刻版 ≈8.04 ms → **约 7.6×**，
底盘控制环上限 125 Hz vs 原厂约 950 Hz。

### 4.3 当前 `ReadReg` 校验与重试状态

原厂 `cds_servo_ReadReg`（`0x5908`，308 B）自带 3 次重试 + 严格帧校验
（`0xFF 0xFF` 前缀、id 匹配、len == count+2）。旧报告声称复刻版未校验前缀/长度，但与已存在的源码校验不符，现予更正。
当前源码最多 3 次完整请求/读取：只接受 `FF FF` 前缀、ID 匹配、收到的精确字节数
`count + 6`，并要求响应 length 字段为 `count + 2`，失败时返回 `EIO`。

---

## 5. ADC / IO / RGB：协议一致

### 5.1 ADC 字节序（复刻版已修正）

原厂 `ADC_GetAll`（`0xb238`，124 B）：

```asm
b280: ldrb r0, [r3, #2]        ; rx[2]
b284: ldrb r2, [r3, #1]        ; rx[1]
b28c: orr  r2, r2, r0, lsl #8  ; rx[1] | (rx[2] << 8)
b290: strh r2, [r1, #2]!
```

→ **小端**：`values[i] = rx[2i+1] | (rx[2i+2] << 8)`，共 10 个 `uint16_t`，
命令字节 `0x80`，SPI 传输 21 字节。

复刻版当前实现与此**完全一致**（`libuptech.c:117`）。历史上下载目录里那份 `libuptech.c`
曾写成大端，属已修复的 bug，不是设计差异。

### 5.2 SPI 参数

原厂：`spi_open(pi, 2, 1000000, 256)` → 通道 2（SPI1 CE2 = GPIO16）、1 MHz、`mode` 参数
`256 = 0x100`（pigpio 的 flag 位：bit8 = CS active low）。
复刻版：`/dev/spidev1.0`，`SPI_MODE_0`，8 bit，1 MHz。**等价**。

### 5.3 IO / RGB 帧格式

| 操作 | 原厂命令字节 | 长度 | 复刻版 |
|---|---|---|---|
| `adc_io_InputGetAll` | `0x96`，xfer 2 B，取 `rx[1]` | 2 | 一致 |
| `adc_io_ModeGetAll` | `0x95`，xfer 2 B，取 `rx[1]` | 2 | 一致 |
| `adc_io_ModeSetAll` | `0x15` + mode | 2 | 一致 |
| `adc_io_Set(i,l)` | `0x18+i` + level，i∈[0,7] | 2 | 一致 |
| `adc_io_SetAll` | `0x18` + 8 个 bit（LSB=IO0） | 9 | 一致 |
| `adc_led_set(idx,rgb)` | `0x1F + idx*4`, B, G, R 各 1 字节 | 4 | 一致 |

`adc_io_ModeSet` 原厂实现是"先 `ModeGetAll` 读回、改单个 bit、再 `ModeSetAll`"，复刻版相同。

注意 `adc_led_set` 参数顺序：`(rgb & 0xFF)` 先发、再 `>>8`、再 `>>16` ——
即 **B, G, R** 顺序，与 `0xRRGGBB` 命名相反，复刻版正确复现了这一点。

---

## 6. LCD / UGUI：原厂完整实现，复刻版为桩

原厂实现是一套完整的 μGUI 移植 + ST7789 类 SPI 屏驱动：

| 组件 | 原厂 | 复刻版 |
|---|---|---|
| UGUI 控件（窗口/按钮/文本框/图片） | 119 个 `UG_*`，18,524 B | 桩，返回 `ENOTSUP` |
| `lcd_open` | 2,136 B（面板初始化序列） | `ENOTSUP` |
| `LCD_Refresh` | 188 B，整屏 SPI 刷 | `ENOTSUP` |
| 字库 | `font_4x6` … `font_24x40` 全 15 套（`.rodata` 约 120 KB） | 无 |

`LCD_Refresh`（`0x6034`）关键路径：设 `CASET(0x2A)`/`RASET(0x2B)` 窗口后，
`LCD_WR_REG(0x2C)` + `spi_write(fd, framebuffer, 25600)` ——
一次刷 **25,600 字节**（160×160×1 B 或等效 RGB565 半屏）。
`LCD_WR_REG` 通过 `gpio_write(pi, 27, 0/1)` 翻转 DC 引脚。

逆向时对旧构建的记录曾把保留桩的数量写成 45，这是把导出 FUNC 总数误当成 LCD/UGUI 桩数。
当前源码实际保留 18 个 LCD/UGUI 入口：除 `lcd_close` 返回 0 外，其余
17 个返回 `ENOTSUP`。这里的 45 应读作**导出 FUNC 总数**，不是 LCD/UGUI 桩数。

---

## 7. 导出符号覆盖

```
原厂导出 FUNC:   264
复刻版导出 FUNC:  45
复刻版独有的符号:   0   ← 复刻版完全是原厂的子集
原厂缺失于复刻版: 219
```

上表是逆向时对检出旧 `.so` 的静态符号对比。当前源码不扩展 ABI，仍是 45 个导出 FUNC；
“45 个导出 FUNC”不等于整个库完全 stripped：旧 `.so` 的 `.dynsym` 和节表仍存在。
由于新姿态代码使用 `sqrt` / `atan2`，当前 Makefile 显式增加 `-lm`，避免依赖宿主进程恰好预加载
libm；这属于链接依赖修订，不改变导出函数集合。

历史分类估算如下；总缺失数 219 来自符号集合差集，类别计数未重新逐个去重，不应视为本轮已核验的精确分类：

| 类别 | 数量 | 说明 |
|---|---|---|
| UGUI / LCD 控件 | 119 | 兼容入口共保留 18 个 LCD/UGUI 函数；17 个返回 `ENOTSUP`，`lcd_close` 返回 0 |
| MPU/DMP 低层 | 77 | `dmp_*` 27 个、`mpu_*` 46 个、`inv_*` 2 个、`get_st_6500_biases` 等 |
| 底盘/串口内部 | 15 | `drv_serial_*`、`set_port_attr`、`SERVO_BUS_TX_*`、`Write_Data`、`Read_Data` |
| 定时器/信号 | 4 | `set_timer`、`signal_handler`、`usleep_s`、`msleep_s` |
| MPU I2C 低层 | 4 | `MPU_I2C_readBytes/writeBytes`、`mpu6500_read/write_block` |

**影响**：任何直接调用 `dmp_*`、`UG_*` 控件、`drv_serial_*` 或 `mpu_*` 的既有 Python 代码，
在复刻版上会 `AttributeError`。

---

## 8. 数值常量对照（便于校验复刻）

| 常量 | 值 | 位置 | 含义 |
|---|---|---|---|
| accel 灵敏度取用 | `*(u16*)(&accel_sens+20)` | `0xc088` | 运行时由 `mpu_get_accel_sens` 填充 |
| gyro 灵敏度取用 | `*(f32*)(&gyro_sens+16)` | `0xc0e8` | 同上 |
| 四元数定标 | `0x30800000` = 9.3132e-10 = 2^-30 | `0xbe70` | q30 s16 → float |
| 弧度转度 | double 57.3（`0xbe68`，注：硬编码近似值，非精确 180/π） | `0xbe68` | 乘 57.3 |
| quat 符号修正 | `0x3f800000` = 1.0f | `0xbe78` | +1 偏置 |
| quat 符号修正 | `0xc0000000` = -2.0f | `0xbe74` | |
| DMP 初始化延时 | `0x186a0` = 100000 µs | `0xc050` | 复位后等待 |
| DMP 预热延时 | `0x2710` = 10000 µs | `0xc070` | 预热循环 |
| 定时器 | `100` | `0xc028` | → 10000 µs (100 Hz) |
| 底盘发送延时 | `50` / `1000` µs | `0x5874`/`0x588c` | TX 后 / 转向后 |
| 底盘帧长上限 | `512` 字节栈缓冲 | `0x57b8` | |

---

## 9. 结论与建议

### 9.1 复刻版做对的部分

- **协议层完全正确**：ADC 小端解码、IO/RGB 命令字与位序、SCS 帧格式与校验和，逐字节对得上；
- **SPI/I²C 直连内核**，单次调用与裸系统调用持平（实测 195.3 vs 195.1 µs；882.9 vs 881.9 µs），
  相比原厂的 pigpio socket 往返**没有劣势**；
- **ABI 兼容性策略正确**：保留 45 个导出 FUNC（其中 18 个 LCD/UGUI 入口），老代码能加载；
- **失败安全设计更好**：风扇独立 Socket + 租约、状态机回差、GUI 只读默认，原厂无对应机制。

### 9.2 三处实质差距（按修复性价比排序）

| 优先级 | 问题 | 影响 | 建议 |
|---|---|---|---|
| **P0** | 历史 `tcdrain` 路径 | 旧构建单帧约 8 ms | Pi 候选已用有界 `TIOCOUTQ` 编译，pty 回归通过；真实零速帧未运行 |
| **P1** | 历史 MPU 同步读、无姿态角 | 旧构建单次约 890 µs；无姿态 | Pi 候选默认 10 ms 缓存及软件互补姿态实测通过；不是 DMP，yaw 会漂移，物理倾斜精度待验收 |
| **P2** | LCD/UGUI 全桩 | 无显示能力 | 若要屏，需移植 μGUI + 面板初始化序列（`lcd_open` 2 KB） |

### 9.3 边界与未验证项

- **未做运行时对比**：原厂库是 ARM32，需 32 位树莓派 OS 才能加载；本次所有"原厂性能"
  均为**静态分析推断**，未在原厂环境实测。复刻版数值为实机实测。
- **ARM32 伪代码未生成**：REA 在 Windows 的 Ghidra provider 仅支持 PE；ELF 布局分析需 Linux。
  本报告结论基于 `objdump` 汇编 + 手工语义还原，未使用反编译器。
- **DMP 固件未提取**：`dmp_load_motion_driver_firmware` 引用的固件数据在 `.rodata`，
  未逐字节还原，如需移植需单独处理。
- **LCD 面板型号未确证**：`lcd_open` 的 2136 B 初始化序列未逐条解析，仅从分辨率/刷屏尺寸推断。
- **当前候选硬件验收边界**：本轮候选已在 Pi 编译，缓存与同步 MPU 实测、pty 串口协议、mock 并发/fork/线程退出，
  ABI 相同性和 GUI 七页只读加载检查均通过。脚本 `_verify_servo_timing.py` 需要向真实电机总线发送合法零速帧，
  所以本轮未运行；真实电机负载响应、物理倾斜方向/精度仍未验证。
- **候选与发布产物**：候选在 `~/openlib-work`，SHA-256 为
  `47844441d1ddfe5e80ca5ce277655edec6ad731597e39627c20c35359989ee32`；仓库 `libuptech.so` 和 `~/libuptech64`
  尚未更新。候选源代码工作区的 MinGW 不能构建 Linux ARM64，目标编译在 Pi 完成。
- **构建与生命周期边界**：调用方停止所有 API 线程后才可 `dlclose()`；本轮新增测试在 Pi 上验证了
  12 次 load/open/dlclose 循环没有增加 fd 或线程数。fork prepare 锁住全部库内互斥量，child 清理 MPU 快照并恢复锁；
  fork 后继续使用库通过模拟 I2C 测试，应用进程仍建议 fork 后立即 `exec()`。

---

## 10. 本轮候选验证（2026-10-30）

- AArch64 Pi 以 `-Wall -Wextra -Werror` 构建成功，候选 74,744 B。
- 缓存 `Get_Accel` / `Get_Gyro` 中位均 3.1 µs，同步均 888.2 µs；前者返回最近一次约 10 ms 周期样本。
- 实机加速度模长约 1.03–1.05 g；脚本临时改量程后恢复初始 ±2 g / ±250 dps。
- 45 个 FUNC 导出名与旧实机库完全一致，0 个 OBJECT 导出。
- 风扇 5 项、pty 6 项、MPU/UART mock 的比例/度数/轴向/四调用线程/fork 回归通过。
- ThreadSanitizer 并发回归通过；TSAN runtime 不支持多线程 fork 后启动新线程，所以 sanitizer 运行显式跳过 fork，普通回归保留此项。
- 12 次加载/采样/卸载后 fd 与线程数无增长；调用方仍须在卸载前停止所有 API 线程。
- Pi Qt offscreen 渲染七页并导出实际传感器 JSON；默认姿态有效，缓存关闭时显示 ENOTSUP 且有效角度为空。
- 本轮 GUI 检查跳过打开 CDS 总线，防止退出急停逻辑发真实零速帧；未操作 IO/RGB 输出及风扇 PWM。

完整候选记录见 [OPTIMIZATION-VALIDATION.md](OPTIMIZATION-VALIDATION.md)。本节保留初版候选的测试边界；后续右轮修复及用户功能验收结果见本文开头最新修订与验收记录。仓库预编译 `.so` 已同步为停机修复版，实机 `~/libuptech64` 安装目录仍保留旧版；真实 TX/GPIO4 波形未归档，未采集倾角误差和 yaw 漂移率。

## 附：复现方式

```bash
# 原厂库来源（树莓派）
/home/shumeipai/uptech_download/pyuptech_pkg/pyuptech/lib/libuptech.so
sha256 75dfa819e8e43d786453150c9536d14747a5594686fdccf5a64278ca597ddf22

# 全量反汇编
objdump -d libuptech.so > full.asm

# ELF 结构 / 符号 / 导出对比（本仓库 _re/ 目录）
python elfinfo.py        orig-libuptech-armhf.so
python dumpfuncs.py      orig-libuptech-armhf.so
python dynsym.py         replica.so
python compare_exports.py
```
