#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE
#endif

#include <errno.h>
#include <fcntl.h>
#include <linux/gpio.h>
#include <linux/i2c-dev.h>
#include <linux/spi/spidev.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static int spi_fd = -1;
static int uart_fd = -1;
static int direction_fd = -1;
static int i2c_fd = -1;
static pthread_mutex_t spi_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t servo_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t i2c_lock = PTHREAD_MUTEX_INITIALIZER;

/* The vendor library refreshes accel/gyro in the background and lets the
   getters return the last snapshot, which is what keeps a single read near
   1 us instead of the ~890 us an I2C transaction costs on this board.
   UPTECH_MPU_CACHE_MS=0 restores the old synchronous behaviour. */
typedef struct {
    int16_t accel[3];
    int16_t gyro[3];
    float accel_sensitivity;
    float gyro_sensitivity;
    int64_t timestamp_us;
    unsigned int sequence;
    int valid;
} mpu_snapshot;

static mpu_snapshot mpu_cache;
static pthread_mutex_t mpu_cache_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_t mpu_thread;
static int mpu_thread_started;
static atomic_int mpu_thread_stop;
static int mpu_cache_ms = 10;
static pthread_once_t mpu_cache_config_once = PTHREAD_ONCE_INIT;

/* Sensitivity must follow the range actually written to the chip.  Dividing a
   +-2 g sample by the +-8 g constant would over-report by 4x, so keep the
   active sensitivity next to the register value. */
static float accel_sensitivity = 4096.0f;
static float gyro_sensitivity = 16.4f;
static int accel_fsr = 8;
static unsigned int gyro_fsr = 2000;

static int mpu_cache_snapshot(mpu_snapshot *out);
static void mpu_cache_stop(void);
static int mpu_cache_start(void);
static int accel_sensitivity_for(int fsr, float *sensitivity);
static int gyro_sensitivity_for(unsigned int fsr, float *sensitivity);

static const char *env_or_default(const char *name, const char *fallback) {
    const char *value = getenv(name);
    return value != NULL && value[0] != '\0' ? value : fallback;
}

static int env_int(const char *name, int fallback) {
    const char *value = getenv(name);
    if (value == NULL || value[0] == '\0') {
        return fallback;
    }
    char *end = NULL;
    long parsed = strtol(value, &end, 0);
    return end != value && *end == '\0' ? (int)parsed : fallback;
}

static void mpu_cache_config_init(void) {
    mpu_cache_ms = env_int("UPTECH_MPU_CACHE_MS", 10);
    if (mpu_cache_ms < 0) {
        mpu_cache_ms = 0;
    }
}

static int mpu_cache_interval_ms(void) {
    int error = pthread_once(&mpu_cache_config_once, mpu_cache_config_init);
    if (error != 0) {
        errno = error;
        return 0;
    }
    return mpu_cache_ms;
}

static int spi_message(const uint8_t *tx, uint8_t *rx, size_t length) {
    if (spi_fd < 0 || tx == NULL || length == 0) {
        errno = ENODEV;
        return -1;
    }
    struct spi_ioc_transfer transfer = {
        .tx_buf = (uintptr_t)tx,
        .rx_buf = (uintptr_t)rx,
        .len = (uint32_t)length,
        .speed_hz = 1000000,
        .bits_per_word = 8,
    };
    pthread_mutex_lock(&spi_lock);
    int result = ioctl(spi_fd, SPI_IOC_MESSAGE(1), &transfer);
    pthread_mutex_unlock(&spi_lock);
    return result < 0 ? -1 : 0;
}

int adc_io_open(void) {
    pthread_mutex_lock(&spi_lock);
    if (spi_fd >= 0) {
        int result = spi_fd;
        pthread_mutex_unlock(&spi_lock);
        return result;
    }
    /* The board's ADC uses the physical SPI1 CE2 pin (GPIO16).  The Pi boot
       overlay maps that pin as the only chip-select, so Linux exposes it as
       spidev1.0 while leaving GPIO18 free for the fan PWM. */
    const char *device = env_or_default("UPTECH_SPI_DEVICE", "/dev/spidev1.0");
    int fd = open(device, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        pthread_mutex_unlock(&spi_lock);
        return -1;
    }
    uint8_t mode = SPI_MODE_0;
    uint8_t bits = 8;
    uint32_t speed = 1000000;
    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed) < 0) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        pthread_mutex_unlock(&spi_lock);
        return -1;
    }
    spi_fd = fd;
    pthread_mutex_unlock(&spi_lock);
    return fd;
}

int adc_io_close(void) {
    pthread_mutex_lock(&spi_lock);
    int result = 0;
    if (spi_fd >= 0) {
        result = close(spi_fd);
        spi_fd = -1;
    }
    pthread_mutex_unlock(&spi_lock);
    return result;
}

int ADC_GetAll(uint16_t *values) {
    if (values == NULL) {
        errno = EINVAL;
        return -1;
    }
    uint8_t tx[21] = {0x80};
    uint8_t rx[21] = {0};
    if (spi_message(tx, rx, sizeof(tx)) < 0) {
        return -1;
    }
    for (size_t index = 0; index < 10; ++index) {
        /* The controller returns each 16-bit sample least-significant byte
           first.  Values 0..8 are external ADC channels; value 9 is the
           board power-voltage measurement retained by the vendor ABI. */
        values[index] = (uint16_t)(rx[index * 2 + 1] |
                                   ((uint16_t)rx[index * 2 + 2] << 8));
    }
    return 0;
}

int adc_io_InputGetAll(void) {
    uint8_t tx[2] = {0x96, 0};
    uint8_t rx[2] = {0};
    return spi_message(tx, rx, sizeof(tx)) < 0 ? -1 : rx[1];
}

int adc_io_ModeGetAll(uint8_t *mode) {
    if (mode == NULL) {
        errno = EINVAL;
        return -1;
    }
    uint8_t tx[2] = {0x95, 0};
    uint8_t rx[2] = {0};
    if (spi_message(tx, rx, sizeof(tx)) < 0) {
        return -1;
    }
    *mode = rx[1];
    return 0;
}

int adc_io_ModeSetAll(int mode) {
    uint8_t tx[2] = {0x15, (uint8_t)mode};
    return spi_message(tx, NULL, sizeof(tx));
}

int adc_io_ModeSet(unsigned int index, int mode) {
    if (index > 7 || (mode != 0 && mode != 1)) {
        errno = EINVAL;
        return -1;
    }
    uint8_t current = 0;
    if (adc_io_ModeGetAll(&current) < 0) {
        return -1;
    }
    if (mode == 0) {
        current = (uint8_t)(current & ~(1u << index));
    } else {
        current = (uint8_t)(current | (1u << index));
    }
    return adc_io_ModeSetAll(current);
}

int adc_io_Set(unsigned int index, int level) {
    if (index > 7) {
        errno = EINVAL;
        return -1;
    }
    uint8_t tx[2] = {(uint8_t)(0x18 + index), (uint8_t)level};
    return spi_message(tx, NULL, sizeof(tx));
}

int adc_io_SetAll(unsigned int levels) {
    uint8_t tx[9] = {0x18};
    for (size_t index = 0; index < 8; ++index) {
        tx[index + 1] = (uint8_t)((levels >> index) & 1u);
    }
    return spi_message(tx, NULL, sizeof(tx));
}

int adc_led_set(int index, int rgb) {
    if (index < 0 || index > 1) {
        errno = EINVAL;
        return -1;
    }
    uint8_t tx[4] = {
        (uint8_t)(0x1f + index * 4),
        (uint8_t)rgb,
        (uint8_t)(rgb >> 8),
        (uint8_t)(rgb >> 16),
    };
    return spi_message(tx, NULL, sizeof(tx));
}

static int direction_open(void) {
    if (direction_fd >= 0) {
        return 0;
    }
    const char *device = env_or_default("UPTECH_GPIOCHIP", "/dev/gpiochip4");
    int chip_fd = open(device, O_RDONLY | O_CLOEXEC);
    if (chip_fd < 0) {
        return -1;
    }
    struct gpiohandle_request request = {0};
    request.lineoffsets[0] = (uint32_t)env_int("UPTECH_DIRECTION_GPIO", 4);
    request.flags = GPIOHANDLE_REQUEST_OUTPUT;
    request.default_values[0] = 0;
    request.lines = 1;
    snprintf(request.consumer_label, sizeof(request.consumer_label), "libuptech64");
    int result = ioctl(chip_fd, GPIO_GET_LINEHANDLE_IOCTL, &request);
    int saved_errno = errno;
    close(chip_fd);
    if (result < 0) {
        errno = saved_errno;
        return -1;
    }
    direction_fd = request.fd;
    return 0;
}

static int direction_set(int transmit) {
    if (direction_fd < 0) {
        errno = ENODEV;
        return -1;
    }
    struct gpiohandle_data values = {0};
    values.values[0] = transmit ? 1 : 0;
    return ioctl(direction_fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL, &values);
}

static int write_all(int fd, const uint8_t *data, size_t length) {
    size_t offset = 0;
    while (offset < length) {
        ssize_t written = write(fd, data + offset, length - offset);
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            return -1;
        }
        offset += (size_t)written;
    }
    return 0;
}

static int64_t monotonic_us(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000000 + now.tv_nsec / 1000;
}

static int wait_until_us(int64_t deadline) {
    int64_t remaining;
    while ((remaining = deadline - monotonic_us()) > 0) {
        struct timespec delay = {
            .tv_sec = 0,
            .tv_nsec = (long)remaining * 1000,
        };
        if (nanosleep(&delay, NULL) == 0 || errno == EINTR) {
            continue;
        }
        return -1;
    }
    return 0;
}

/* TIOCOUTQ only reports the software queue: a complete frame may still be in
   the UART FIFO when it reaches zero. Keep TX enabled for a full frame's wire
   time after that observation, plus 20 us for direction switching. At 1 Mbps
   with 8N1 this is 10 us per byte. This bounded wait avoids tcdrain()'s fixed
   tty polling delay without treating software-queue-empty as wire-empty. */
static int wait_uart_output_empty(int fd, size_t frame_length) {
    int64_t wire_time = (int64_t)frame_length * 10;
    int64_t budget = 1000 + wire_time;
    int64_t deadline = monotonic_us() + budget;
    while (monotonic_us() < deadline) {
        int pending = -1;
        if (ioctl(fd, TIOCOUTQ, &pending) < 0) {
            int saved_errno = errno;
            if (saved_errno == EINTR) {
                continue;
            }
            if (saved_errno == ENOTTY || saved_errno == EINVAL ||
                saved_errno == EOPNOTSUPP) {
                return wait_until_us(deadline);
            }
            errno = saved_errno;
            return -1;
        }
        if (pending <= 0) {
            return wait_until_us(monotonic_us() + wire_time + 20);
        }
        if (wait_until_us(monotonic_us() + 1) < 0) {
            return -1;
        }
    }
    errno = ETIMEDOUT;
    return -1;
}

int cds_servo_open(void) {
    pthread_mutex_lock(&servo_lock);
    if (uart_fd >= 0) {
        int result = uart_fd;
        pthread_mutex_unlock(&servo_lock);
        return result;
    }
    if (direction_open() < 0) {
        pthread_mutex_unlock(&servo_lock);
        return -1;
    }
    const char *device = env_or_default("UPTECH_UART_DEVICE", "/dev/ttyAMA0");
    int fd = open(device, O_RDWR | O_NOCTTY | O_CLOEXEC);
    if (fd < 0) {
        pthread_mutex_unlock(&servo_lock);
        return -1;
    }
    struct termios config;
    if (tcgetattr(fd, &config) < 0) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        pthread_mutex_unlock(&servo_lock);
        return -1;
    }
    cfmakeraw(&config);
#ifdef B1000000
    cfsetispeed(&config, B1000000);
    cfsetospeed(&config, B1000000);
#else
#error "B1000000 is required for the CDS servo bus"
#endif
    config.c_cflag |= CLOCAL | CREAD;
    config.c_cflag &= ~(CSTOPB | CRTSCTS);
    config.c_cc[VMIN] = 0;
    config.c_cc[VTIME] = 1;
    if (tcsetattr(fd, TCSANOW, &config) < 0) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        pthread_mutex_unlock(&servo_lock);
        return -1;
    }
    tcflush(fd, TCIOFLUSH);
    direction_set(0);
    uart_fd = fd;
    pthread_mutex_unlock(&servo_lock);
    return fd;
}

int cds_servo_close(void) {
    pthread_mutex_lock(&servo_lock);
    int result = 0;
    if (uart_fd >= 0) {
        direction_set(0);
        result = close(uart_fd);
        uart_fd = -1;
    }
    if (direction_fd >= 0) {
        close(direction_fd);
        direction_fd = -1;
    }
    pthread_mutex_unlock(&servo_lock);
    return result;
}

static int servo_send_locked(uint8_t id, uint8_t length, uint8_t instruction,
                             const uint8_t *parameters) {
    if (uart_fd < 0 || length < 2) {
        errno = ENODEV;
        return -1;
    }
    size_t parameter_count = (size_t)length - 2;
    size_t frame_length = (size_t)length + 4;
    uint8_t frame[260];
    if (frame_length > sizeof(frame)) {
        errno = EINVAL;
        return -1;
    }
    frame[0] = 0xff;
    frame[1] = 0xff;
    frame[2] = id;
    frame[3] = length;
    frame[4] = instruction;
    uint8_t checksum = (uint8_t)(id + length + instruction);
    for (size_t index = 0; index < parameter_count; ++index) {
        frame[index + 5] = parameters[index];
        checksum = (uint8_t)(checksum + parameters[index]);
    }
    frame[frame_length - 1] = (uint8_t)~checksum;

    /* The vendor firmware retries the whole exchange up to three times; a
       single dropped frame on the shared half-duplex bus otherwise leaves the
       caller with a silent no-op.  Failures are bounded so a dead bus still
       returns within a few milliseconds. */
    int saved_errno = EIO;
    for (int attempt = 0; attempt < 3; ++attempt) {
        tcflush(uart_fd, TCIFLUSH);
        if (direction_set(1) < 0) {
            saved_errno = errno;
            continue;
        }
        if (write_all(uart_fd, frame, frame_length) < 0 ||
            wait_uart_output_empty(uart_fd, frame_length) < 0) {
            saved_errno = errno;
            direction_set(0);
            continue;
        }
        if (direction_set(0) < 0) {
            saved_errno = errno;
            continue;
        }
        usleep(1000);
        return 0;
    }
    errno = saved_errno;
    return -1;
}

int cds_servo_SendFrame(int id, int length, int instruction, const uint8_t *parameters) {
    pthread_mutex_lock(&servo_lock);
    int result = servo_send_locked((uint8_t)id, (uint8_t)length,
                                   (uint8_t)instruction, parameters);
    pthread_mutex_unlock(&servo_lock);
    return result;
}

/* Read exactly one status packet.  The vendor build demands read() == count+6
   and rejects anything else, so a short or over-long frame must not be
   accepted as valid data. */
static ssize_t read_status_locked(uint8_t *response, size_t expected) {
    size_t received = 0;
    while (received < expected) {
        ssize_t count = read(uart_fd, response + received, expected - received);
        if (count > 0) {
            received += (size_t)count;
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0 && errno != EAGAIN) {
            return -1;
        }
        break;
    }
    return (ssize_t)received;
}

int cds_servo_ReadReg(int id, int address, uint8_t *output, int count) {
    if (output == NULL || count < 0 || count > 250) {
        errno = EINVAL;
        return -1;
    }
    uint8_t parameters[2] = {(uint8_t)address, (uint8_t)count};
    uint8_t response[260] = {0};
    size_t expected = (size_t)count + 6;
    int saved_errno = EIO;
    pthread_mutex_lock(&servo_lock);
    for (int attempt = 0; attempt < 3; ++attempt) {
        if (servo_send_locked((uint8_t)id, 4, 2, parameters) < 0) {
            saved_errno = errno;
            continue;
        }
        ssize_t received = read_status_locked(response, expected);
        if (received == (ssize_t)expected && response[0] == 0xff &&
            response[1] == 0xff && response[2] == (uint8_t)id &&
            response[3] == (uint8_t)(count + 2)) {
            memcpy(output, response + 5, (size_t)count);
            pthread_mutex_unlock(&servo_lock);
            return count;
        }
        saved_errno = EIO;
    }
    pthread_mutex_unlock(&servo_lock);
    errno = saved_errno;
    return -1;
}

int cds_servo_SetMode(int id, int mode) {
    if (mode != 0 && mode != 1) {
        errno = EINVAL;
        return -1;
    }
    uint8_t parameters[5] = {0x06, 0, 0, 0, 0};
    if (mode == 0) {
        parameters[3] = 0xff;
        parameters[4] = 0x03;
    }
    return cds_servo_SendFrame(id, 7, 3, parameters);
}

int cds_servo_SetAngle(int id, int angle, int speed) {
    if (angle < 0 || angle >= 1024 || speed < 0 || speed >= 1024) {
        errno = EINVAL;
        return -1;
    }
    uint8_t parameters[5] = {
        0x1e,
        (uint8_t)angle,
        (uint8_t)(angle >> 8),
        (uint8_t)speed,
        (uint8_t)(speed >> 8),
    };
    return cds_servo_SendFrame(id, 7, 3, parameters);
}

int cds_servo_SetSpeed(int id, int speed) {
    if (speed < -1023 || speed > 1023) {
        errno = EINVAL;
        return -1;
    }
    unsigned int encoded = (unsigned int)(speed < 0 ? -speed : speed);
    if (speed < 0) {
        encoded |= 0x400;
    }
    uint8_t parameters[3] = {
        0x20,
        (uint8_t)encoded,
        (uint8_t)(encoded >> 8),
    };
    return cds_servo_SendFrame(id, 5, 3, parameters);
}

int cds_servo_GetPos(int id) {
    uint8_t position[2] = {0};
    if (cds_servo_ReadReg(id, 0x24, position, 2) != 2) {
        return -1;
    }
    return position[0] | ((int)position[1] << 8);
}

static int i2c_open_sensor(void) {
    if (i2c_fd >= 0) {
        return 0;
    }
    const char *device = env_or_default("UPTECH_I2C_DEVICE", "/dev/i2c-1");
    int fd = open(device, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    int address = env_int("UPTECH_MPU_ADDRESS", 0x68);
    if (ioctl(fd, I2C_SLAVE, address) < 0) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        return -1;
    }
    i2c_fd = fd;
    return 0;
}

static int i2c_write_register(uint8_t reg, uint8_t value) {
    uint8_t data[2] = {reg, value};
    return write_all(i2c_fd, data, sizeof(data));
}

static int i2c_read_registers(uint8_t reg, uint8_t *data, size_t length) {
    if (write_all(i2c_fd, &reg, 1) < 0) {
        return -1;
    }
    size_t offset = 0;
    while (offset < length) {
        ssize_t count = read(i2c_fd, data + offset, length - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return -1;
        }
        offset += (size_t)count;
    }
    return 0;
}

/* The chip keeps its range across process restarts, so adopt whatever is
   actually programmed instead of assuming the power-on default.  Otherwise a
   +-2 g chip divided by the +-8 g constant reports 4x too much. */
static void mpu_sync_range_from_chip(void) {
    uint8_t config = 0;
    if (i2c_read_registers(0x1b, &config, 1) == 0) {
        static const unsigned int ranges[4] = {250, 500, 1000, 2000};
        unsigned int selected = ranges[(config >> 3) & 0x3];
        float sensitivity;
        if (gyro_sensitivity_for(selected, &sensitivity) == 0) {
            gyro_fsr = selected;
            gyro_sensitivity = sensitivity;
        }
    }
    if (i2c_read_registers(0x1c, &config, 1) == 0) {
        static const int ranges[4] = {2, 4, 8, 16};
        int selected = ranges[(config >> 3) & 0x3];
        float sensitivity;
        if (accel_sensitivity_for(selected, &sensitivity) == 0) {
            accel_fsr = selected;
            accel_sensitivity = sensitivity;
        }
    }
}

int mpu6500_open(void) {
    pthread_mutex_lock(&i2c_lock);
    int result = i2c_open_sensor();
    if (result == 0) {
        uint8_t identity = 0;
        result = i2c_read_registers(0x75, &identity, 1);
        if (result == 0 && identity != 0x70 && identity != 0x68) {
            errno = ENODEV;
            result = -1;
        }
    }
    if (result == 0) {
        mpu_sync_range_from_chip();
    }
    pthread_mutex_unlock(&i2c_lock);
    if (result == 0) {
        result = mpu_cache_start();
    }
    return result;
}

int mpu6500_dmp_init(void) {
    pthread_mutex_lock(&i2c_lock);
    int result = i2c_open_sensor();
    if (result == 0) {
        result = i2c_write_register(0x6b, 0x01);
    }
    if (result == 0) {
        result = i2c_write_register(0x1a, 0x02);
    }
    if (result == 0) {
        result = i2c_write_register(0x1b, 0x18);
    }
    if (result == 0) {
        gyro_fsr = 2000;
        gyro_sensitivity = 16.4f;
    }
    if (result == 0) {
        result = i2c_write_register(0x1c, 0x10);
    }
    if (result == 0) {
        accel_fsr = 8;
        accel_sensitivity = 4096.0f;
    }
    if (result == 0) {
        result = i2c_write_register(0x19, 0x00);
    }
    pthread_mutex_unlock(&i2c_lock);
    if (result == 0) {
        result = mpu_cache_start();
    }
    return result;
}

static int accel_sensitivity_for(int fsr, float *sensitivity) {
    switch (fsr) {
    case 2:  *sensitivity = 16384.0f; return 0;
    case 4:  *sensitivity = 8192.0f;  return 0;
    case 8:  *sensitivity = 4096.0f;  return 0;
    case 16: *sensitivity = 2048.0f;  return 0;
    default: errno = EINVAL; return -1;
    }
}

static int gyro_sensitivity_for(unsigned int fsr, float *sensitivity) {
    switch (fsr) {
    case 250:  *sensitivity = 131.0f; return 0;
    case 500:  *sensitivity = 65.5f;  return 0;
    case 1000: *sensitivity = 32.8f;  return 0;
    case 2000: *sensitivity = 16.4f;  return 0;
    default: errno = EINVAL; return -1;
    }
}

static int mpu_read_raw_locked(uint8_t reg, int16_t *raw) {
    uint8_t data[6];
    if (i2c_read_registers(reg, data, sizeof(data)) < 0) {
        return -1;
    }
    for (size_t index = 0; index < 3; ++index) {
        raw[index] = (int16_t)(((uint16_t)data[index * 2] << 8) |
                               data[index * 2 + 1]);
    }
    return 0;
}

/* One lock owns each complete raw sample and its conversion scale.  This
   prevents a range change from publishing half an old frame with the new
   sensitivity. */
static int mpu_read_cache_sample(mpu_snapshot *sample) {
    int result;
    pthread_mutex_lock(&i2c_lock);
    result = i2c_open_sensor();
    if (result == 0) {
        result = mpu_read_raw_locked(0x3b, sample->accel);
    }
    if (result == 0) {
        result = mpu_read_raw_locked(0x43, sample->gyro);
    }
    if (result == 0) {
        sample->accel_sensitivity = accel_sensitivity;
        sample->gyro_sensitivity = gyro_sensitivity;
    }
    pthread_mutex_unlock(&i2c_lock);
    return result;
}

static int mpu_read_vector(uint8_t reg, float *values) {
    if (values == NULL) {
        errno = EINVAL;
        return -1;
    }
    int16_t raw[3];
    float sensitivity;
    pthread_mutex_lock(&i2c_lock);
    int result = i2c_open_sensor();
    if (result == 0) {
        result = mpu_read_raw_locked(reg, raw);
    }
    sensitivity = reg == 0x3b ? accel_sensitivity : gyro_sensitivity;
    pthread_mutex_unlock(&i2c_lock);
    if (result < 0) {
        return -1;
    }
    for (size_t index = 0; index < 3; ++index) {
        values[index] = (float)raw[index] / sensitivity;
    }
    return 0;
}

/* Complementary filter state.  Yaw has no magnetometer reference on this
   board, so it is reported as a drifting relative angle rather than an
   absolute heading. */
static pthread_mutex_t attitude_lock = PTHREAD_MUTEX_INITIALIZER;
static double attitude_pitch;
static double attitude_roll;
static double attitude_yaw;
static int attitude_valid;

static void attitude_update(const int16_t *accel_raw, const int16_t *gyro_raw,
                            double accel_sens, double gyro_sens, double dt) {
    double ax = accel_raw[0] / accel_sens;
    double ay = accel_raw[1] / accel_sens;
    double az = accel_raw[2] / accel_sens;
    double gx = gyro_raw[0] / gyro_sens;
    double gy = gyro_raw[1] / gyro_sens;
    double gz = gyro_raw[2] / gyro_sens;

    /* Keep the integrator sane if the sampling thread was stalled. */
    if (dt < 0.0 || dt > 0.05) {
        dt = 0.01;
    }

    pthread_mutex_lock(&attitude_lock);
    double magnitude = sqrt(ax * ax + ay * ay + az * az);
    double accel_pitch = 0.0;
    double accel_roll = 0.0;
    if (magnitude > 0.1) {
        accel_pitch = atan2(-ax, sqrt(ay * ay + az * az)) * (180.0 / M_PI);
        accel_roll = atan2(ay, az) * (180.0 / M_PI);
    }
    /* The first valid gravity frame defines the initial attitude. */
    if (!attitude_valid) {
        if (magnitude > 0.1) {
            attitude_pitch = accel_pitch;
            attitude_roll = accel_roll;
            attitude_yaw = 0.0;
            attitude_valid = 1;
        }
        pthread_mutex_unlock(&attitude_lock);
        return;
    }

    /* State is in degrees.  Chip x maps to roll, y to pitch and z to yaw,
       matching the accelerometer formulas above.  Yaw is relative because this
       board has no magnetometer. */
    double pitch = attitude_pitch + gy * dt;
    double roll = attitude_roll + gx * dt;
    double yaw = attitude_yaw + gz * dt;
    if (magnitude > 0.1) {
        const double gain = 0.02;
        pitch += gain * (accel_pitch - pitch);
        roll += gain * (accel_roll - roll);
    }

    if (yaw > 180.0) {
        yaw -= 360.0;
    } else if (yaw < -180.0) {
        yaw += 360.0;
    }
    attitude_pitch = pitch;
    attitude_roll = roll;
    attitude_yaw = yaw;
    pthread_mutex_unlock(&attitude_lock);
}

static void mpu_cache_publish(const mpu_snapshot *sample) {
    pthread_mutex_lock(&mpu_cache_lock);
    unsigned int sequence = mpu_cache.sequence + 1;
    mpu_cache = *sample;
    mpu_cache.timestamp_us = monotonic_us();
    mpu_cache.sequence = sequence;
    mpu_cache.valid = 1;
    pthread_mutex_unlock(&mpu_cache_lock);
}

static int mpu_cache_snapshot(mpu_snapshot *out) {
    pthread_mutex_lock(&mpu_cache_lock);
    int valid = mpu_cache.valid;
    if (valid) {
        *out = mpu_cache;
    }
    pthread_mutex_unlock(&mpu_cache_lock);
    return valid;
}

static void *mpu_cache_loop(void *unused) {
    (void)unused;
    int64_t previous_us = 0;
    while (!atomic_load_explicit(&mpu_thread_stop, memory_order_acquire)) {
        mpu_snapshot sample = {0};
        if (mpu_read_cache_sample(&sample) == 0) {
            mpu_cache_publish(&sample);
            int64_t now = monotonic_us();
            double dt =
                previous_us == 0 ? 0.0 : (double)(now - previous_us) / 1000000.0;
            attitude_update(sample.accel, sample.gyro,
                            sample.accel_sensitivity,
                            sample.gyro_sensitivity, dt);
            previous_us = now;
        }
        usleep((useconds_t)mpu_cache_interval_ms() * 1000);
    }
    return NULL;
}

static int mpu_cache_start(void) {
    if (mpu_cache_interval_ms() <= 0) {
        return 0;
    }
    pthread_mutex_lock(&mpu_cache_lock);
    if (mpu_thread_started) {
        pthread_mutex_unlock(&mpu_cache_lock);
        return 0;
    }
    atomic_store_explicit(&mpu_thread_stop, 0, memory_order_release);
    int result = pthread_create(&mpu_thread, NULL, mpu_cache_loop, NULL);
    if (result == 0) {
        mpu_thread_started = 1;
    } else {
        errno = result;
    }
    pthread_mutex_unlock(&mpu_cache_lock);
    return result == 0 ? 0 : -1;
}

static void mpu_cache_stop(void) {
    pthread_mutex_lock(&mpu_cache_lock);
    if (!mpu_thread_started) {
        pthread_mutex_unlock(&mpu_cache_lock);
        return;
    }
    atomic_store_explicit(&mpu_thread_stop, 1, memory_order_release);
    pthread_mutex_unlock(&mpu_cache_lock);
    pthread_join(mpu_thread, NULL);
    pthread_mutex_lock(&mpu_cache_lock);
    mpu_thread_started = 0;
    memset(&mpu_cache, 0, sizeof(mpu_cache));
    pthread_mutex_unlock(&mpu_cache_lock);
    pthread_mutex_lock(&attitude_lock);
    attitude_valid = 0;
    attitude_pitch = 0.0;
    attitude_roll = 0.0;
    attitude_yaw = 0.0;
    pthread_mutex_unlock(&attitude_lock);
}

/* API code never nests these locks.  Fork takes them in this order so the
   child receives a complete bus state with every mutex owned by one thread. */
static void uptech_lock_all(void) {
    pthread_mutex_lock(&spi_lock);
    pthread_mutex_lock(&servo_lock);
    pthread_mutex_lock(&i2c_lock);
    pthread_mutex_lock(&mpu_cache_lock);
    pthread_mutex_lock(&attitude_lock);
}

static void uptech_atfork_release_owned(void) {
    pthread_mutex_unlock(&attitude_lock);
    pthread_mutex_unlock(&mpu_cache_lock);
    pthread_mutex_unlock(&i2c_lock);
    pthread_mutex_unlock(&servo_lock);
    pthread_mutex_unlock(&spi_lock);
}

static void uptech_atfork_prepare(void) {
    uptech_lock_all();
}

static void uptech_atfork_parent(void) {
    uptech_atfork_release_owned();
}

static void uptech_atfork_child(void) {
    /* Inherited fds remain valid, but only the forking thread survives. */
    mpu_thread_started = 0;
    atomic_store_explicit(&mpu_thread_stop, 0, memory_order_release);
    memset(&mpu_cache, 0, sizeof(mpu_cache));
    attitude_valid = 0;
    attitude_pitch = 0.0;
    attitude_roll = 0.0;
    attitude_yaw = 0.0;
    uptech_atfork_release_owned();
}

__attribute__((constructor)) static void uptech_library_init(void) {
    pthread_atfork(uptech_atfork_prepare, uptech_atfork_parent,
                   uptech_atfork_child);
}

/* Callers must stop using the API in all threads before dlclose(). */
__attribute__((destructor)) static void uptech_library_fini(void) {
    mpu_cache_stop();
    adc_io_close();
    cds_servo_close();
    pthread_mutex_lock(&i2c_lock);
    if (i2c_fd >= 0) {
        close(i2c_fd);
        i2c_fd = -1;
    }
    pthread_mutex_unlock(&i2c_lock);
}

static int mpu_cached_vector(const int16_t *raw, float sensitivity, float *values) {
    if (values == NULL) {
        errno = EINVAL;
        return -1;
    }
    for (size_t index = 0; index < 3; ++index) {
        values[index] = (float)raw[index] / sensitivity;
    }
    return 0;
}

int mpu6500_Get_Accel(float *values) {
    mpu_snapshot snapshot;
    if (mpu_cache_interval_ms() > 0 && mpu_cache_snapshot(&snapshot)) {
        return mpu_cached_vector(snapshot.accel, snapshot.accel_sensitivity,
                                 values);
    }
    return mpu_read_vector(0x3b, values);
}

int mpu6500_Get_Gyro(float *values) {
    mpu_snapshot snapshot;
    if (mpu_cache_interval_ms() > 0 && mpu_cache_snapshot(&snapshot)) {
        return mpu_cached_vector(snapshot.gyro, snapshot.gyro_sensitivity,
                                 values);
    }
    return mpu_read_vector(0x43, values);
}

int mpu6500_Get_Attitude(float *values) {
    if (values == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (mpu_cache_interval_ms() <= 0) {
        values[0] = values[1] = values[2] = 0.0f;
        errno = ENOTSUP;
        return -1;
    }
    pthread_mutex_lock(&attitude_lock);
    int valid = attitude_valid;
    values[0] = (float)attitude_pitch;
    values[1] = (float)attitude_roll;
    values[2] = (float)attitude_yaw;
    pthread_mutex_unlock(&attitude_lock);
    if (!valid) {
        values[0] = values[1] = values[2] = 0.0f;
        errno = EAGAIN;
        return -1;
    }
    return 0;
}

int mpu_get_gyro_fsr(uint16_t *fsr) {
    if (fsr == NULL) {
        errno = EINVAL;
        return -1;
    }
    pthread_mutex_lock(&i2c_lock);
    *fsr = (uint16_t)gyro_fsr;
    pthread_mutex_unlock(&i2c_lock);
    return 0;
}

int mpu_get_accel_fsr(int8_t *fsr) {
    if (fsr == NULL) {
        errno = EINVAL;
        return -1;
    }
    pthread_mutex_lock(&i2c_lock);
    *fsr = (int8_t)accel_fsr;
    pthread_mutex_unlock(&i2c_lock);
    return 0;
}

int mpu_set_gyro_fsr(unsigned int fsr) {
    float sensitivity;
    if (gyro_sensitivity_for(fsr, &sensitivity) < 0) {
        return -1;
    }
    uint8_t value = fsr == 250 ? 0x00 : fsr == 500 ? 0x08 : fsr == 1000 ? 0x10 : 0x18;
    pthread_mutex_lock(&i2c_lock);
    int result = i2c_open_sensor();
    if (result == 0) {
        result = i2c_write_register(0x1b, value);
    }
    if (result == 0) {
        gyro_fsr = fsr;
        gyro_sensitivity = sensitivity;
    }
    pthread_mutex_unlock(&i2c_lock);
    return result;
}

int mpu_set_accel_fsr(int fsr) {
    float sensitivity;
    if (accel_sensitivity_for(fsr, &sensitivity) < 0) {
        return -1;
    }
    uint8_t value = fsr == 2 ? 0x00 : fsr == 4 ? 0x08 : fsr == 8 ? 0x10 : 0x18;
    pthread_mutex_lock(&i2c_lock);
    int result = i2c_open_sensor();
    if (result == 0) {
        result = i2c_write_register(0x1c, value);
    }
    if (result == 0) {
        accel_fsr = fsr;
        accel_sensitivity = sensitivity;
    }
    pthread_mutex_unlock(&i2c_lock);
    return result;
}

int lcd_open(int direction) { (void)direction; errno = ENOTSUP; return -1; }
int lcd_close(void) { return 0; }
int LCD_Refresh(void) { errno = ENOTSUP; return -1; }
int LCD_SetFont(int font) { (void)font; errno = ENOTSUP; return -1; }
int UG_SetForecolor(int color) { (void)color; errno = ENOTSUP; return -1; }
int UG_SetBackcolor(int color) { (void)color; errno = ENOTSUP; return -1; }
int UG_FillScreen(int color) { (void)color; errno = ENOTSUP; return -1; }
int UG_PutString(int x, int y, const char *text) {
    (void)x; (void)y; (void)text; errno = ENOTSUP; return -1;
}
int UG_FillFrame(int x1, int y1, int x2, int y2, int color) {
    (void)x1; (void)y1; (void)x2; (void)y2; (void)color; errno = ENOTSUP; return -1;
}
int UG_FillRoundFrame(int x1, int y1, int x2, int y2, int radius, int color) {
    (void)x1; (void)y1; (void)x2; (void)y2; (void)radius; (void)color; errno = ENOTSUP; return -1;
}
int UG_FillCircle(int x, int y, int radius, int color) {
    (void)x; (void)y; (void)radius; (void)color; errno = ENOTSUP; return -1;
}
int UG_DrawMesh(int x1, int y1, int x2, int y2, int color) {
    (void)x1; (void)y1; (void)x2; (void)y2; (void)color; errno = ENOTSUP; return -1;
}
int UG_DrawFrame(int x1, int y1, int x2, int y2, int color) {
    (void)x1; (void)y1; (void)x2; (void)y2; (void)color; errno = ENOTSUP; return -1;
}
int UG_DrawRoundFrame(int x1, int y1, int x2, int y2, int radius, int color) {
    (void)x1; (void)y1; (void)x2; (void)y2; (void)radius; (void)color; errno = ENOTSUP; return -1;
}
int UG_DrawPixel(int x, int y, int color) {
    (void)x; (void)y; (void)color; errno = ENOTSUP; return -1;
}
int UG_DrawCircle(int x, int y, int radius, int color) {
    (void)x; (void)y; (void)radius; (void)color; errno = ENOTSUP; return -1;
}
int UG_DrawArc(int x, int y, int radius, int sector, int color) {
    (void)x; (void)y; (void)radius; (void)sector; (void)color; errno = ENOTSUP; return -1;
}
int UG_DrawLine(int x1, int y1, int x2, int y2, int color) {
    (void)x1; (void)y1; (void)x2; (void)y2; (void)color; errno = ENOTSUP; return -1;
}
