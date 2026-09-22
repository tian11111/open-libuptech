#define _DEFAULT_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/gpio.h>
#include <linux/i2c-dev.h>
#include <linux/spi/spidev.h>
#include <pthread.h>
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

    tcflush(uart_fd, TCIFLUSH);
    if (direction_set(1) < 0 || write_all(uart_fd, frame, frame_length) < 0 ||
        tcdrain(uart_fd) < 0) {
        direction_set(0);
        return -1;
    }
    usleep(50);
    if (direction_set(0) < 0) {
        return -1;
    }
    usleep(1000);
    return 0;
}

int cds_servo_SendFrame(int id, int length, int instruction, const uint8_t *parameters) {
    pthread_mutex_lock(&servo_lock);
    int result = servo_send_locked((uint8_t)id, (uint8_t)length,
                                   (uint8_t)instruction, parameters);
    pthread_mutex_unlock(&servo_lock);
    return result;
}

static ssize_t read_status_locked(uint8_t *response, size_t capacity) {
    size_t received = 0;
    for (int attempt = 0; attempt < 4 && received < capacity; ++attempt) {
        ssize_t count = read(uart_fd, response + received, capacity - received);
        if (count > 0) {
            received += (size_t)count;
            if (received >= 4 && received >= (size_t)response[3] + 4) {
                break;
            }
        } else if (count < 0 && errno != EINTR && errno != EAGAIN) {
            return -1;
        }
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
    pthread_mutex_lock(&servo_lock);
    int result = servo_send_locked((uint8_t)id, 4, 2, parameters);
    ssize_t received = result < 0 ? -1 : read_status_locked(response, sizeof(response));
    pthread_mutex_unlock(&servo_lock);
    if (received < count + 6 || response[0] != 0xff || response[1] != 0xff ||
        response[2] != (uint8_t)id || response[3] != (uint8_t)(count + 2)) {
        errno = EIO;
        return -1;
    }
    memcpy(output, response + 5, (size_t)count);
    return count;
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
    pthread_mutex_unlock(&i2c_lock);
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
        result = i2c_write_register(0x1c, 0x10);
    }
    if (result == 0) {
        result = i2c_write_register(0x19, 0x00);
    }
    pthread_mutex_unlock(&i2c_lock);
    return result;
}

static int mpu_read_vector(uint8_t reg, float *values, float scale) {
    if (values == NULL) {
        errno = EINVAL;
        return -1;
    }
    uint8_t data[6];
    pthread_mutex_lock(&i2c_lock);
    int result = i2c_open_sensor();
    if (result == 0) {
        result = i2c_read_registers(reg, data, sizeof(data));
    }
    pthread_mutex_unlock(&i2c_lock);
    if (result < 0) {
        return -1;
    }
    for (size_t index = 0; index < 3; ++index) {
        int16_t raw = (int16_t)(((uint16_t)data[index * 2] << 8) |
                                data[index * 2 + 1]);
        values[index] = raw / scale;
    }
    return 0;
}

int mpu6500_Get_Accel(float *values) {
    return mpu_read_vector(0x3b, values, 4096.0f);
}

int mpu6500_Get_Gyro(float *values) {
    return mpu_read_vector(0x43, values, 16.4f);
}

int mpu6500_Get_Attitude(float *values) {
    if (values != NULL) {
        values[0] = 0.0f;
        values[1] = 0.0f;
        values[2] = 0.0f;
    }
    errno = ENOTSUP;
    return -1;
}

int mpu_get_gyro_fsr(uint16_t *fsr) {
    if (fsr == NULL) {
        errno = EINVAL;
        return -1;
    }
    *fsr = 2000;
    return 0;
}

int mpu_get_accel_fsr(int8_t *fsr) {
    if (fsr == NULL) {
        errno = EINVAL;
        return -1;
    }
    *fsr = 8;
    return 0;
}

int mpu_set_gyro_fsr(unsigned int fsr) {
    if (fsr != 250 && fsr != 500 && fsr != 1000 && fsr != 2000) {
        errno = EINVAL;
        return -1;
    }
    uint8_t value = fsr == 250 ? 0x00 : fsr == 500 ? 0x08 : fsr == 1000 ? 0x10 : 0x18;
    pthread_mutex_lock(&i2c_lock);
    int result = i2c_open_sensor();
    if (result == 0) {
        result = i2c_write_register(0x1b, value);
    }
    pthread_mutex_unlock(&i2c_lock);
    return result;
}

int mpu_set_accel_fsr(int fsr) {
    if (fsr != 2 && fsr != 4 && fsr != 8 && fsr != 16) {
        errno = EINVAL;
        return -1;
    }
    uint8_t value = fsr == 2 ? 0x00 : fsr == 4 ? 0x08 : fsr == 8 ? 0x10 : 0x18;
    pthread_mutex_lock(&i2c_lock);
    int result = i2c_open_sensor();
    if (result == 0) {
        result = i2c_write_register(0x1c, value);
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
