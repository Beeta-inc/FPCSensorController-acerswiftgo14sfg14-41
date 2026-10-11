#!/usr/bin/env python3
"""
tools/test_state_monitor.py - Monitor sensor internal state before, during, and after ARM
"""

import sys
import time
import struct
import ctypes

LIBUSB_ENDPOINT_IN = 0x80
LIBUSB_REQUEST_TYPE_VENDOR = (0x02 << 5)
LIBUSB_RECIPIENT_DEVICE = 0x00
LIBUSB_ENDPOINT_OUT = 0x00

CTRL_OUT = LIBUSB_ENDPOINT_OUT | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE
CTRL_IN  = LIBUSB_ENDPOINT_IN  | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE

def main():
    libusb = ctypes.CDLL('libusb-1.0.so.0')
    libusb.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
    libusb.libusb_control_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
        ctypes.c_uint16, ctypes.c_uint16, ctypes.c_char_p,
        ctypes.c_uint16, ctypes.c_uint
    ]
    libusb.libusb_control_transfer.restype = ctypes.c_int
    libusb.libusb_bulk_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_char_p,
        ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint
    ]
    libusb.libusb_bulk_transfer.restype = ctypes.c_int

    ctx = ctypes.c_void_p()
    libusb.libusb_init(ctypes.byref(ctx))
    h = libusb.libusb_open_device_with_vid_pid(ctx, 0x10a5, 0xe340)
    if not h:
        print("Device not found")
        return

    if libusb.libusb_kernel_driver_active(h, 0) == 1:
        libusb.libusb_detach_kernel_driver(h, 0)
    libusb.libusb_claim_interface(h, 0)

    buf = ctypes.create_string_buffer(4096)
    trans = ctypes.c_int()

    def get_state(label):
        r = libusb.libusb_control_transfer(h, CTRL_IN, 0x50, 1, 0, buf, 42, 1000)
        if r == 42:
            data = buf.raw[:42]
            mode = data[41]
            pwr = data[38]
            zones = struct.unpack('<H', data[10:12])[0]
            print(f"  [{label}] mode={mode}, pwr=0x{pwr:02x}, zones=0x{zones:04x}, raw={data[34:42].hex(' ')}")
        else:
            print(f"  [{label}] GET_STATE failed: {r}")

    # 1. Abort
    libusb.libusb_control_transfer(h, CTRL_OUT, 0x03, 1, 0, None, 0, 1000)
    time.sleep(0.05)
    get_state("After ABORT")

    # 2. Init
    session_id = struct.pack('<I', 0x0077FF12)
    libusb.libusb_control_transfer(h, CTRL_OUT, 0x01, 1, 0, session_id, 4, 1000)
    libusb.libusb_bulk_transfer(h, 0x81, buf, 4096, ctypes.byref(trans), 1000)
    print(f"[+] Init done, resp={trans.value} bytes")
    get_state("After INIT")

    # 3. S_STATE (0x10)
    libusb.libusb_control_transfer(h, CTRL_OUT, 0x08, 0x10, 0, None, 0, 1000)
    get_state("After S_STATE 0x10")

    # 4. Get TLS key (0x0B)
    r = libusb.libusb_control_transfer(h, CTRL_IN, 0x0B, 0, 0, buf, 121, 1000)
    print(f"[+] TLS key ret={r}")
    get_state("After TLS Key")

    # 5. ARM (0x02)
    cap_id = struct.pack('<I', 0x0701100F)
    r = libusb.libusb_control_transfer(h, CTRL_OUT, 0x02, 1, 0, cap_id, 4, 1000)
    print(f"[+] ARM ret={r}")
    get_state("Immediately After ARM")

    print("\n>>> MONITORING SENSOR STATE FOR 10 SECONDS... <<<")
    print(">>> (Try touching / tapping / pressing the sensor during this time) <<<\n")
    start = time.time()
    while time.time() - start < 10:
        get_state(f"t={time.time()-start:.1f}s")
        # Check bulk EP 0x81 with 200ms timeout
        r = libusb.libusb_bulk_transfer(h, 0x81, buf, 4096, ctypes.byref(trans), 200)
        if r == 0 and trans.value > 0:
            print(f"\n[!!! BULK PACKET RECEIVED: {trans.value} bytes: {buf.raw[:trans.value].hex(' ')} !!!]\n")
        time.sleep(0.3)

    libusb.libusb_release_interface(h, 0)
    libusb.libusb_close(h)
    libusb.libusb_exit(ctx)

if __name__ == '__main__':
    main()
