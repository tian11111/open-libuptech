"""Servo frame protocol checks against a pseudo-terminal.

The library is redirected to a pty via UPTECH_UART_DEVICE, so the real
/dev/ttyAMA0 motor bus is never written to.  GPIO4 may still toggle as the
bus direction line, which is harmless because no device is listening on the
pty.  Run on the Pi:

    UPTECH_LIB=./libuptech.so python3 -m unittest tests.test_servo_protocol
"""

import ctypes
import errno
import os
import select
import sys
import threading
import time
import unittest


def library_path():
    return os.environ.get("UPTECH_LIB", "./libuptech.so")


class ServoProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(os, "openpty"):
            raise unittest.SkipTest("pseudo-terminals are unavailable")
        cls.library = ctypes.CDLL(library_path(), use_errno=True)
        cls.library.cds_servo_open.restype = ctypes.c_int
        cls.library.cds_servo_close.restype = ctypes.c_int
        cls.library.cds_servo_SendFrame.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8),
        ]
        cls.library.cds_servo_SendFrame.restype = ctypes.c_int
        cls.library.cds_servo_ReadReg.argtypes = [
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
        ]
        cls.library.cds_servo_ReadReg.restype = ctypes.c_int

    def setUp(self):
        self.master, self.slave = os.openpty()
        os.set_blocking(self.master, False)
        os.environ["UPTECH_UART_DEVICE"] = os.ttyname(self.slave)
        if self.library.cds_servo_open() < 0:
            self.skipTest(f"cannot open pty device, errno={ctypes.get_errno()}")
        # Drop anything the open path left behind.
        while select.select([self.master], [], [], 0)[0]:
            os.read(self.master, 256)

    def tearDown(self):
        try:
            self.library.cds_servo_close()
        finally:
            os.close(self.master)
            os.close(self.slave)

    def drain(self):
        while select.select([self.master], [], [], 0)[0]:
            os.read(self.master, 256)

    def inject_reply(self, payload):
        """The library flushes stale input before each attempt, so the reply
        has to arrive while it is already waiting in read()."""
        stop = threading.Event()

        def worker():
            while not stop.is_set():
                try:
                    os.write(self.master, payload)
                except OSError:
                    return
                stop.wait(0.005)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        self.addCleanup(thread.join, 1.0)

    def test_frame_layout_matches_scs_protocol(self):
        params = (ctypes.c_uint8 * 3)(0x20, 0x10, 0x00)
        self.assertEqual(self.library.cds_servo_SendFrame(7, 5, 3, params), 0)
        ready, _, _ = select.select([self.master], [], [], 0.5)
        self.assertTrue(ready, "no frame reached the pty")
        frame = os.read(self.master, 64)
        self.assertEqual(frame[0:2], b"\xff\xff")
        self.assertEqual(frame[2], 7)              # id
        self.assertEqual(frame[3], 5)              # length
        self.assertEqual(frame[4], 3)              # instruction
        self.assertEqual(frame[5:8], bytes(params))
        checksum = (7 + 5 + 3 + sum(params)) & 0xFF
        self.assertEqual(frame[8], (~checksum) & 0xFF)
        self.assertEqual(len(frame), 9)

    def test_send_frame_rejects_short_length(self):
        params = (ctypes.c_uint8 * 1)(0)
        self.assertEqual(self.library.cds_servo_SendFrame(7, 1, 3, params), -1)
        self.assertEqual(ctypes.get_errno(), errno.ENODEV)

    def test_send_frame_does_not_block_for_tcdrain_interval(self):
        """tcdrain() cost a fixed ~8 ms on this tty layer; the polled wait must
        return far sooner for a short frame."""
        params = (ctypes.c_uint8 * 3)(0x20, 0x00, 0x00)
        self.library.cds_servo_SendFrame(7, 5, 3, params)  # warm up
        self.drain()
        samples = []
        for _ in range(20):
            start = time.perf_counter()
            self.assertEqual(self.library.cds_servo_SendFrame(7, 5, 3, params), 0)
            samples.append((time.perf_counter() - start) * 1e6)
            self.drain()
        median = sorted(samples)[len(samples) // 2]
        self.assertLess(median, 2000.0, f"median {median:.0f} us looks like tcdrain")

    def test_read_reg_accepts_valid_reply(self):
        # Reply: FF FF id len addr data.. checksum, len == count + 2
        reply = bytes([0xFF, 0xFF, 7, 4, 0x24, 0x11, 0x22, 0x00])
        self.inject_reply(reply)
        output = (ctypes.c_uint8 * 2)()
        self.assertEqual(self.library.cds_servo_ReadReg(7, 0x24, output, 2), 2)
        self.assertEqual(list(output), [0x11, 0x22])

    def test_read_reg_rejects_bad_prefix(self):
        self.inject_reply(bytes([0x00, 0xFF, 7, 4, 0x24, 0x11, 0x22, 0x00]))
        output = (ctypes.c_uint8 * 2)()
        self.assertEqual(self.library.cds_servo_ReadReg(7, 0x24, output, 2), -1)
        self.assertEqual(ctypes.get_errno(), errno.EIO)

    def test_read_reg_rejects_wrong_length_field(self):
        self.inject_reply(bytes([0xFF, 0xFF, 7, 9, 0x24, 0x11, 0x22, 0x00]))
        output = (ctypes.c_uint8 * 2)()
        self.assertEqual(self.library.cds_servo_ReadReg(7, 0x24, output, 2), -1)
        self.assertEqual(ctypes.get_errno(), errno.EIO)


if __name__ == "__main__":
    unittest.main()
