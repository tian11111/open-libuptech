#define _DEFAULT_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <unistd.h>

/* Include the implementation only in this test executable. All I2C reads and
   writes below use an in-memory register model, never a hardware device. */
static ssize_t mock_read(int fd, void *data, size_t length);
static ssize_t mock_write(int fd, const void *data, size_t length);
static int mock_ioctl(int fd, unsigned long request, ...);
#define read mock_read
#define write mock_write
#define ioctl mock_ioctl
#include "../libuptech.c"
#undef read
#undef write
#undef ioctl

static uint8_t registers[256];
static uint8_t selected_register;
static int uart_error;
static int interrupted_once;

static ssize_t mock_write(int fd, const void *data, size_t length) {
    assert(fd == i2c_fd);
    const uint8_t *bytes = data;
    assert(length == 1 || length == 2);
    selected_register = bytes[0];
    if (length == 2) registers[bytes[0]] = bytes[1];
    return (ssize_t)length;
}

static ssize_t mock_read(int fd, void *data, size_t length) {
    assert(fd == i2c_fd);
    uint8_t *bytes = data;
    if (length == 1) {
        bytes[0] = registers[selected_register];
    } else {
        assert(length == 6);
        static const float accel_scales[] = {16384, 8192, 4096, 2048};
        static const float gyro_scales[] = {131, 65.5f, 32.8f, 16.4f};
        float scale = selected_register == 0x3b
            ? accel_scales[(registers[0x1c] >> 3) & 3]
            : 10 * gyro_scales[(registers[0x1b] >> 3) & 3];
        memset(bytes, 0, length);
        int16_t raw = (int16_t)lroundf(scale);
        bytes[4] = (uint8_t)((uint16_t)raw >> 8);
        bytes[5] = (uint8_t)raw;
    }
    return (ssize_t)length;
}

static int mock_ioctl(int fd, unsigned long request, ...) {
    (void)fd;
    if (request == TIOCOUTQ) {
        if (interrupted_once) {
            interrupted_once = 0;
            errno = EINTR;
            return -1;
        }
        if (uart_error) { errno = uart_error; return -1; }
        va_list args;
        va_start(args, request);
        *va_arg(args, int *) = 0;
        va_end(args);
        return 0;
    }
    errno = ENOTTY;
    return -1;
}

static void reset_attitude(void) {
    attitude_valid = 0;
    attitude_pitch = attitude_roll = attitude_yaw = 0;
}

static void test_attitude(void) {
    int16_t still[] = {0, 0, 4096}, zero[] = {0, 0, 0};
    int16_t pitch30[] = {-2048, 0, 3547};
    int16_t roll30[] = {0, 2048, 3547};
    reset_attitude();
    attitude_update(pitch30, zero, 4096, 16.4f, 0);
    assert(fabs(attitude_pitch - 30) < 0.02 && fabs(attitude_roll) < 0.02);
    reset_attitude();
    attitude_update(roll30, zero, 4096, 16.4f, 0);
    assert(fabs(attitude_roll - 30) < 0.02 && fabs(attitude_pitch) < 0.02);
    for (int axis = 0; axis < 3; ++axis) {
        reset_attitude();
        attitude_update(still, zero, 4096, 16.4f, 0);
        int16_t rate[] = {0, 0, 0};
        rate[axis] = 1640; /* 100 degrees/second for one second */
        for (int i = 0; i < 100; ++i)
            attitude_update(zero, rate, 4096, 16.4f, 0.01);
        double angle = axis == 0 ? attitude_roll
            : axis == 1 ? attitude_pitch : attitude_yaw;
        assert(fabs(angle - 100) < 0.02);
    }
    reset_attitude();
}

static void *reader(void *unused) {
    (void)unused;
    for (int i = 0; i < 1500; ++i) {
        float accel[3], gyro[3];
        int8_t accel_range;
        uint16_t gyro_range;
        assert(mpu6500_Get_Accel(accel) == 0);
        assert(mpu6500_Get_Gyro(gyro) == 0);
        assert(fabsf(accel[2] - 1) < 0.001f);
        assert(fabsf(gyro[2] - 10) < 0.02f);
        assert(mpu_get_accel_fsr(&accel_range) == 0);
        assert(mpu_get_gyro_fsr(&gyro_range) == 0);
    }
    return NULL;
}

static void *range_writer(void *unused) {
    (void)unused;
    const int accel[] = {2, 4, 8, 16};
    const unsigned gyro[] = {250, 500, 1000, 2000};
    for (int i = 0; i < 1500; ++i) {
        assert(mpu_set_accel_fsr(accel[i % 4]) == 0);
        assert(mpu_set_gyro_fsr(gyro[i % 4]) == 0);
    }
    return NULL;
}

static void test_cache_and_concurrency(void) {
    mpu_snapshot sample = {0};
    float values[3];
    assert(mpu_read_cache_sample(&sample) == 0);
    mpu_cache_publish(&sample);
    assert(mpu_set_accel_fsr(2) == 0);
    assert(mpu_set_gyro_fsr(250) == 0);
    /* A cached frame must keep the scale used when it was acquired. */
    assert(mpu6500_Get_Accel(values) == 0 && fabsf(values[2] - 1) < 0.001f);
    assert(mpu6500_Get_Gyro(values) == 0 && fabsf(values[2] - 10) < 0.02f);
    assert(mpu_cache_start() == 0);
    pthread_t threads[4];
    for (int i = 0; i < 3; ++i)
        assert(pthread_create(&threads[i], NULL, reader, NULL) == 0);
    assert(pthread_create(&threads[3], NULL, range_writer, NULL) == 0);
    for (int i = 0; i < 4; ++i) assert(pthread_join(threads[i], NULL) == 0);
    puts("cache scale and four concurrent callers OK");
}

static atomic_int lock_owned;
static void *hold_i2c_lock(void *unused) {
    (void)unused;
    pthread_mutex_lock(&i2c_lock);
    atomic_store(&lock_owned, 1);
    usleep(30000);
    pthread_mutex_unlock(&i2c_lock);
    return NULL;
}

static void test_fork(void) {
    pthread_t holder;
    assert(pthread_create(&holder, NULL, hold_i2c_lock, NULL) == 0);
    while (!atomic_load(&lock_owned)) usleep(100);
    pid_t child = fork();
    assert(child >= 0);
    if (child == 0) {
        alarm(3);
        float values[3];
        assert(mpu6500_open() == 0);
        assert(mpu6500_Get_Accel(values) == 0);
        assert(fabsf(values[2] - 1) < 0.001f);
        mpu_cache_stop();
        _exit(0);
    }
    assert(pthread_join(holder, NULL) == 0);
    int status;
    assert(waitpid(child, &status, 0) == child);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    mpu_cache_stop();
    assert(!mpu_thread_started && !mpu_cache.valid);
    puts("fork with an owned I2C lock and sampler shutdown OK");
}

static void test_uart_errors(void) {
    uart_error = EBADF;
    assert(wait_uart_output_empty(-1, 9) == -1 && errno == EBADF);
    uart_error = EIO;
    assert(wait_uart_output_empty(-1, 9) == -1 && errno == EIO);
    uart_error = ENOTTY;
    int64_t start = monotonic_us();
    assert(wait_uart_output_empty(-1, 9) == 0);
    assert(monotonic_us() - start >= 1090);
    uart_error = 0;
    interrupted_once = 1;
    assert(wait_uart_output_empty(-1, 9) == 0);
    puts("UART errors, timed fallback and EINTR retry OK");
}

int main(void) {
    setenv("UPTECH_MPU_CACHE_MS", "1", 1);
    registers[0x75] = 0x70;
    registers[0x1b] = 0x18;
    registers[0x1c] = 0x10;
    i2c_fd = open("/dev/null", O_RDWR | O_CLOEXEC);
    assert(i2c_fd >= 0);
    test_attitude();
    test_uart_errors();
    test_cache_and_concurrency();
    if (getenv("UPTECH_TEST_NO_FORK") == NULL) {
        test_fork();
    } else {
        mpu_cache_stop();
        puts("fork omitted for the ThreadSanitizer runtime");
    }
    puts("MPU mock regressions OK");
    return 0;
}
