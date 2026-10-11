#!/usr/bin/env python3
"""
tools/test_touch.py - Test ARM, touch detection, and image capture on 10a5:e340.
"""

import sys
import time
import struct
import ctypes

LIBUSB_ENDPOINT_IN = 0x80
LIBUSB_REQUEST_TYPE_VENDOR = (0x02 << 5)
LIBUSB_RECIPIENT_DEVICE = 0x00
LIBUSB_ENDPOINT_OUT = 0x00

CTRL_OUT = LIBUSB_ENDPOINT_OUT | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE # 0x40
CTRL_IN  = LIBUSB_ENDPOINT_IN  | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE # 0xC0

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

def test_touch():
    print("=== Testing ARM, Touch Detection & Capture on 10a5:e340 ===")
    usb = LibUSB()
    try:
        handle = usb.open(0x10a5, 0xe340)
        usb.claim(handle, 0)
        print("[+] Device opened & claimed.")

        # Init sequence
        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_ABORT, 0, 0, None, 0, 1000)
        session_id = struct.pack('<I', 0x12345678)
        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_INIT, 1, 0, session_id, 4, 1000)
        
        bulk_buf = ctypes.create_string_buffer(4096)
        transferred = ctypes.c_int(0)
        ret = usb.lib.libusb_bulk_transfer(handle, EP_IN, bulk_buf, 4096, ctypes.byref(transferred), 1000)
        print(f"[+] Init result: {transferred.value} bytes read")

        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_INDICATE_S_STATE, FPC_HOST_MS_S0, 0, None, 0, 1000)
        key_buf = ctypes.create_string_buffer(128)
        usb.lib.libusb_control_transfer(handle, CTRL_IN, 0x0B, 0, 0, key_buf, 121, 1000)
        print("[+] Init sequence finished.")

        # Send ARM
        cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
        print(f"[*] Sending ARM command (0x02, wValue=1, cap_id=0x{FPC_CAPTUREID_RESERVED:08x})...")
        ret = usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_ARM, 1, 0, cap_id, 4, 1000)
        print(f"[+] ARM transfer ret: {ret} bytes transferred")

        print("\n=======================================================")
        print(">>> SENSOR IS NOW ARMED AND WAITING FOR TOUCH! <<<")
        print(">>> PLACE YOUR FINGER FIRMLY ON THE SENSOR NOW! <<<")
        print("=======================================================\n")
        sys.stdout.flush()

        start = time.time()
        touched = False
        while time.time() - start < 60:
            ret = usb.lib.libusb_bulk_transfer(handle, EP_IN, bulk_buf, 4096, ctypes.byref(transferred), 500)
            if ret == 0 and transferred.value > 0:
                print(f"\n[!!! SUCCESS: TOUCH EVENT DETECTED ON EP 0x81! ({transferred.value} bytes) !!!]")
                raw = bulk_buf.raw[:transferred.value]
                print(f"      HEX: {raw.hex(' ')}")
                if transferred.value >= 12:
                    cmdid, length, status = struct.unpack('<III', raw[:12])
                    print(f"      Decoded LE: cmdid={cmdid} (0x{cmdid:02x}), length={length}, status={status}")
                    cmdid_be, length_be, status_be = struct.unpack('>III', raw[:12])
                    print(f"      Decoded BE: cmdid={cmdid_be} (0x{cmdid_be:02x}), length={length_be}, status={status_be}")
                
                # Now trigger GET_IMG (0x09)
                print("\n[*] Sending FPC_CMD_GET_IMG (0x09)...")
                ret = usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_GET_IMG, 0, 0, None, 0, 1000)
                print(f"[+] GET_IMG returned: {ret}")

                # Read image records
                print("[*] Reading image chunks from EP 0x81...")
                total_img_bytes = 0
                record_count = 0
                while True:
                    ret = usb.lib.libusb_bulk_transfer(handle, EP_IN, bulk_buf, 4096, ctypes.byref(transferred), 1000)
                    if ret == 0 and transferred.value > 0:
                        record_count += 1
                        total_img_bytes += transferred.value
                        rec_raw = bulk_buf.raw[:transferred.value]
                        if transferred.value >= 12:
                            r_cmdid, r_len, r_stat = struct.unpack('<III', rec_raw[:12])
                            print(f"    Record {record_count}: {transferred.value} bytes (cmdid={r_cmdid}, len={r_len})")
                        else:
                            print(f"    Record {record_count}: {transferred.value} bytes")
                    else:
                        print(f"    Done reading image (ret={ret}, read {record_count} records, {total_img_bytes} total bytes)")
                        break

                touched = True
                break
            elif ret == -7: # timeout
                sys.stdout.write(".")
                sys.stdout.flush()
            else:
                print(f"\n[!] Bulk transfer returned error: {ret}")
                break

        if not touched:
            print("\n[-] Timed out after 60 seconds with no touch detected.")

        usb.release(handle, 0)
        usb.close(handle)
        print("\n=== Test finished. ===")
    finally:
        usb.exit()

if __name__ == '__main__':
    test_touch()
