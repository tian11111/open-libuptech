"""Measure the tty transmit-wait cost on /dev/ttyAMA0 without driving motors.

The payload deliberately lacks the SCS 0xFF 0xFF header, so every servo on the
bus discards it.  This isolates the difference between tcdrain() and polled
TIOCOUTQ for the same byte count.
"""

import fcntl
import statistics
import struct
import termios
import time

TIOCOUTQ = 0x5411
DEVICE = "/dev/ttyAMA0"
PAYLOAD = bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0xFF])  # invalid SCS frame


def configure(fd):
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = termios.B1000000
    attrs[5] = termios.B1000000
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)


def pending(fd):
    return struct.unpack("i", fcntl.ioctl(fd, TIOCOUTQ, struct.pack("i", 0)))[0]


def run(mode):
    fd = open(DEVICE, "r+b", buffering=0)
    configure(fd)
    samples = []
    for _ in range(200):
        start = time.perf_counter()
        fd.write(PAYLOAD)
        if mode == "tcdrain":
            termios.tcdrain(fd)
        elif mode == "poll":
            deadline = time.perf_counter() + 0.002
            while pending(fd) > 0 and time.perf_counter() < deadline:
                time.sleep(0.000001)
            time.sleep(0.00002)
        samples.append((time.perf_counter() - start) * 1e6)
    fd.close()
    samples.sort()
    return statistics.median(samples), samples[int(len(samples) * 0.99)]


def main():
    for mode in ("write", "tcdrain", "poll"):
        median, p99 = run(mode)
        print(f"{mode:8s} p50={median:8.1f} us  p99={p99:8.1f} us")


if __name__ == "__main__":
    main()
