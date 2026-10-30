#define _DEFAULT_SOURCE
#include <assert.h>
#include <errno.h>
#include <linux/gpio.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static int fake_clock_gettime(clockid_t clock, struct timespec *value);
static int fake_nanosleep(const struct timespec *delay, struct timespec *left);
static int fake_usleep(useconds_t delay);
static ssize_t fake_write(int fd, const void *data, size_t length);
static int fake_ioctl(int fd, unsigned long request, ...);
static int fake_tcflush(int fd, int queue);
#define clock_gettime fake_clock_gettime
#define nanosleep fake_nanosleep
#define usleep fake_usleep
#define write fake_write
#define ioctl fake_ioctl
#define tcflush fake_tcflush
#include "../libuptech.c"
#undef clock_gettime
#undef nanosleep
#undef usleep
#undef write
#undef ioctl
#undef tcflush

static int64_t now_us, wire_end_us, queue_empty_us, direction_down_us;
static int transmitting, ioctl_error, ioctl_eintr, sleep_eintr, writes;
static uint8_t last_frame[260];
static size_t last_length;

static int fake_clock_gettime(clockid_t clock, struct timespec *value) {
    assert(clock == CLOCK_MONOTONIC);
    *value = (struct timespec){now_us / 1000000, now_us % 1000000 * 1000};
    return 0;
}

static int fake_nanosleep(const struct timespec *delay, struct timespec *left) {
    (void)left;
    if (sleep_eintr) { sleep_eintr = 0; errno = EINTR; return -1; }
    now_us += delay->tv_sec * 1000000 + delay->tv_nsec / 1000;
    return 0;
}

static int fake_usleep(useconds_t delay) { now_us += delay; return 0; }

static ssize_t fake_write(int fd, const void *data, size_t length) {
    assert(fd == uart_fd && transmitting);
    assert(length <= sizeof(last_frame));
    memcpy(last_frame, data, length);
    last_length = length;
    wire_end_us = (queue_empty_us > now_us ? queue_empty_us : now_us)
        + (int64_t)length * 10;
    ++writes;
    return (ssize_t)length;
}

static int fake_ioctl(int fd, unsigned long request, ...) {
    va_list args;
    va_start(args, request);
    if (request == GPIOHANDLE_SET_LINE_VALUES_IOCTL) {
        assert(fd == direction_fd);
        struct gpiohandle_data *value = va_arg(args, struct gpiohandle_data *);
        transmitting = value->values[0];
        if (!transmitting) direction_down_us = now_us;
    } else {
        assert(fd == uart_fd && request == TIOCOUTQ);
        if (ioctl_eintr) {
            ioctl_eintr = 0; va_end(args); errno = EINTR; return -1;
        }
        if (ioctl_error) { va_end(args); errno = ioctl_error; return -1; }
        *va_arg(args, int *) = now_us < queue_empty_us ? 1 : 0;
    }
    va_end(args);
    return 0;
}

static int fake_tcflush(int fd, int queue) {
    assert(fd == uart_fd && queue == TCIFLUSH);
    return 0;
}

static void reset(void) {
    now_us = wire_end_us = queue_empty_us = direction_down_us = 0;
    transmitting = ioctl_error = ioctl_eintr = sleep_eintr = writes = 0;
    uart_fd = 101;
    direction_fd = 102;
}

static void assert_complete_frame(void) {
    assert(!transmitting);
    assert(direction_down_us >= wire_end_us + 20);
    assert(now_us >= direction_down_us + 1000);
    unsigned sum = 0;
    for (size_t i = 2; i < last_length; ++i) sum += last_frame[i];
    assert((sum & 0xff) == 0xff);
}

int main(void) {
    reset();
    assert(cds_servo_SetSpeed(8, -1023) == 0);
    assert_complete_frame(); /* TIOCOUTQ is zero while the whole frame is in FIFO. */
    const uint8_t reverse[] = {255, 255, 8, 5, 3, 32, 255, 7, 201};
    assert(last_length == sizeof(reverse));
    assert(memcmp(last_frame, reverse, sizeof(reverse)) == 0);
    assert(cds_servo_SetSpeed(8, 0) == 0);
    assert_complete_frame();
    const uint8_t stop[] = {255, 255, 8, 5, 3, 32, 0, 0, 207};
    assert(memcmp(last_frame, stop, sizeof(stop)) == 0);
    assert(cds_servo_SetSpeed(7, 1023) == 0);
    assert_complete_frame();
    assert(cds_servo_SetSpeed(7, 0) == 0);
    assert_complete_frame();
    assert(cds_servo_SetSpeed(8, -1024) == -1 && errno == EINVAL);
    assert(writes == 4);

    reset();
    queue_empty_us = 100;
    ioctl_eintr = sleep_eintr = 1;
    assert(cds_servo_SetSpeed(8, 0) == 0);
    assert_complete_frame();

    reset();
    uint8_t parameters[253] = {0};
    assert(cds_servo_SendFrame(8, 255, 3, parameters) == 0);
    assert_complete_frame();

    reset();
    ioctl_error = ENOTTY;
    assert(cds_servo_SetSpeed(8, 0) == 0);
    assert_complete_frame();

    reset();
    ioctl_error = EIO;
    assert(cds_servo_SetSpeed(8, 0) == -1 && errno == EIO);
    assert(writes == 3 && !transmitting);
    uart_fd = direction_fd = -1; /* Destructor must not close real descriptors. */
    puts("UART full-frame direction hold, speed encoding and errors OK");
    return 0;
}
