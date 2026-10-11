#!/usr/bin/env python3
"""
tools/interactive_touch.py - 30-second interactive touch detection test.
Arms the sensor and continuously polls EP 0x81.
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

FPC_CMD_INIT = 0x01
FPC_CMD_ARM = 0x02
FPC_CMD_ABORT = 0x03
FPC_CMD_INDICATE_S_STATE = 0x08
FPC_CMD_GET_IMG = 0x09
FPC_HOST_MS_S0 = 0x10
FPC_CAPTUREID_RESERVED = 0x0701100F

EP_IN = 0x81

class LibUSB:
    def __init__(self):
        self.lib = ctypes.CDLL('libusb-1.0.so.0')
        self.lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        self.lib.libusb_control_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
            ctypes.c_uint16, ctypes.c_uint16, ctypes.c_char_p,
            ctypes.c_uint16, ctypes.c_uint
        ]
        self.lib.libusb_control_transfer.restype = ctypes.c_int
        self.lib.libusb_bulk_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_char_p,
            ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint
        ]
        self.lib.libusb_bulk_transfer.restype = ctypes.c_int
        self.ctx = ctypes.c_void_p()
        self.lib.libusb_init(ctypes.byref(self.ctx))

    def open(self, vid, pid):
        handle = self.lib.libusb_open_device_with_vid_pid(self.ctx, vid, pid)
        if not handle:
            raise RuntimeError(f"Device {vid:04x}:{pid:04x} not found.")
        return handle

    def claim(self, handle, interface=0):
        if self.lib.libusb_kernel_driver_active(handle, interface) == 1:
            self.lib.libusb_detach_kernel_driver(handle, interface)
        ret = self.lib.libusb_claim_interface(handle, interface)
        if ret != 0:
            raise RuntimeError(f"libusb_claim_interface failed with error {ret}")

    def release(self, handle, interface=0):
        self.lib.libusb_release_interface(handle, interface)

    def close(self, handle):
        self.lib.libusb_close(handle)

    def exit(self):
        self.lib.libusb_exit(self.ctx)

def main():
    print("=== Interactive Touch Test (30 seconds) ===")
    usb = LibUSB()
    try:
        handle = usb.open(0x10a5, 0xe340)
        usb.claim(handle, 0)

        # 1. ABORT
        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_ABORT, 0, 0, None, 0, 1000)

        # 2. INIT
        session_id = struct.pack('<I', 0x12345678)
        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_INIT, 1, 0, session_id, 4, 1000)

        buf = ctypes.create_string_buffer(4096)
        trans = ctypes.c_int(0)
        usb.lib.libusb_bulk_transfer(handle, EP_IN, buf, 4096, ctypes.byref(trans), 1000)
        print(f"[+] Init event: {trans.value} bytes")

        # 3. S0
        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_INDICATE_S_STATE, FPC_HOST_MS_S0, 0, None, 0, 1000)

        # 4. 0x0B
        usb.lib.libusb_control_transfer(handle, CTRL_IN, 0x0B, 0, 0, buf, 121, 1000)
        print("[+] S0 & TLS key fetched.")

        # 5. ARM
        cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
        ret = usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_ARM, 1, 0, cap_id, 4, 1000)
        print(f"[+] ARM sent (ret = {ret}).")

        print("\n" + "="*60)
        print(">>> SENSOR IS ARMED AND LISTENING ON EP 0x81! <<<")
        print(">>> PLACE YOUR FINGER ON THE SENSOR NOW! <<<")
        print(">>> HOLD FIRMLY, OR LIFT AND TAP FIRMLY! <<<")
        print("="*60 + "\n")
        sys.stdout.flush()

        start = time.time()
        packet_count = 0
        while time.time() - start < 30:
            ret = usb.lib.libusb_bulk_transfer(handle, EP_IN, buf, 4096, ctypes.byref(trans), 250)
            if ret == 0 and trans.value > 0:
                packet_count += 1
                data = buf.raw[:trans.value]
                elapsed = time.time() - start
                print(f"\n[!!! EVENT #{packet_count} RECEIVED at {elapsed:.2f}s ({trans.value} bytes) !!!]")
                print(f"      HEX: {data.hex(' ')}")
                if trans.value >= 12:
                    cmdid, length, status = struct.unpack('<III', data[:12])
                    print(f"      Decoded LE: cmdid=0x{cmdid:02x} ({cmdid}), length={length}, status={status}")
                # Try reading more if available
                while True:
                    ret2 = usb.lib.libusb_bulk_transfer(handle, EP_IN, buf, 4096, ctypes.byref(trans), 200)
                    if ret2 == 0 and trans.value > 0:
                        packet_count += 1
                        more = buf.raw[:trans.value]
                        print(f"      Followup packet #{packet_count} ({trans.value} bytes): {more[:32].hex(' ')}...")
                    else:
                        break
                break
            elif ret == -7:
                sys.stdout.write(".")
                sys.stdout.flush()
            else:
                print(f"\nBulk error: {ret}")
                break

        if packet_count == 0:
            print("\n[-] No touch events received within 30 seconds.")
        else:
            print(f"\n[+] Total packets captured: {packet_count}")

        usb.release(handle, 0)
        usb.close(handle)
        print("=== Test finished ===")
    finally:
        usb.exit()

if __name__ == '__main__':
    main()
