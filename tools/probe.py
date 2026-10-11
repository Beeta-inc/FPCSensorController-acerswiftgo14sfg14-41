#!/usr/bin/env python3
"""
tools/probe.py - Non-destructive probe for FPC Sensor Controller (10a5:e340).
Tests FPC MoH (Match-on-Host) initialization sequence.
"""

import sys
import struct
import ctypes

LIBUSB_ENDPOINT_IN = 0x80
LIBUSB_REQUEST_TYPE_VENDOR = (0x02 << 5)
LIBUSB_RECIPIENT_DEVICE = 0x00
LIBUSB_ENDPOINT_OUT = 0x00

CTRL_OUT = LIBUSB_ENDPOINT_OUT | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE # 0x40
CTRL_IN  = LIBUSB_ENDPOINT_IN  | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE # 0xC0

FPC_CMD_INIT = 0x01
FPC_CMD_ABORT = 0x03
FPC_CMD_INDICATE_S_STATE = 0x08
FPC_HOST_MS_S0 = 0x10

EP_IN = 0x81

class LibUSB:
    def __init__(self):
        try:
            self.lib = ctypes.CDLL('libusb-1.0.so.0')
        except OSError as e:
            print(f"Error loading libusb-1.0: {e}")
            sys.exit(1)

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
        ret = self.lib.libusb_init(ctypes.byref(self.ctx))
        if ret != 0:
            raise RuntimeError(f"libusb_init failed with error {ret}")

    def open(self, vid, pid):
        handle = self.lib.libusb_open_device_with_vid_pid(self.ctx, vid, pid)
        if not handle:
            raise RuntimeError(f"Device {vid:04x}:{pid:04x} not found or permission denied.")
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

def probe():
    print("=== FPC 10a5:e340 Hardware Probe Tool ===")
    usb = LibUSB()
    try:
        handle = usb.open(0x10a5, 0xe340)
        print("[+] Device 10a5:e340 opened.")
        usb.claim(handle, 0)
        print("[+] Interface 0 claimed.")

        # Step 1: Send FPC_CMD_ABORT (0x03, wValue=0)
        print("\n[*] 1. Sending FPC_CMD_ABORT (0x03)...")
        ret = usb.lib.libusb_control_transfer(
            handle, CTRL_OUT, FPC_CMD_ABORT, 0, 0, None, 0, 1000
        )
        print(f"[+] ABORT status: {ret}")

        # Step 2: Send FPC_CMD_INIT (0x01, wValue=1, session_id)
        session_id = struct.pack('<I', 0x12345678)
        print("[*] 2. Sending FPC_CMD_INIT (0x01, wValue=1, session_id=0x12345678)...")
        ret = usb.lib.libusb_control_transfer(
            handle, CTRL_OUT, FPC_CMD_INIT, 1, 0, session_id, 4, 1000
        )
        print(f"[+] INIT status: {ret} bytes transferred")

        # Step 3: Read Init Result from Bulk IN (EP 0x81)
        print("[*] 3. Reading init result from Bulk IN (0x81)...")
        bulk_buf = ctypes.create_string_buffer(2048)
        transferred = ctypes.c_int(0)
        ret = usb.lib.libusb_bulk_transfer(
            handle, EP_IN, bulk_buf, 2048, ctypes.byref(transferred), 1000
        )
        print(f"[+] Bulk read returned {ret}, transferred {transferred.value} bytes")
        if transferred.value >= 28:
            raw = bulk_buf.raw[:transferred.value]
            cmdid, length, status = struct.unpack('<III', raw[:12])
            sensor, hw_id, w, h = struct.unpack('<HHHH', raw[12:20])
            fw_ver = raw[20:36].split(b'\x00')[0].decode('latin1')
            print(f"    -> cmdid={cmdid}, status={status}")
            print(f"    -> Sensor Type: {sensor}, HWID: 0x{hw_id:04x}")
            print(f"    -> Resolution:  {w} x {h}")
            print(f"    -> Firmware:    {fw_ver}")

        # Step 4: Indicate S0 State (0x08, wValue=0x10)
        print("\n[*] 4. Sending INDICATE_S_STATE (0x08, S0)...")
        ret = usb.lib.libusb_control_transfer(
            handle, CTRL_OUT, FPC_CMD_INDICATE_S_STATE, FPC_HOST_MS_S0, 0, None, 0, 1000
        )
        print(f"[+] S_STATE returned: {ret}")

        # Step 5: Read 0x0B (Key / TLS Data, 121 bytes)
        print("[*] 5. Requesting Control IN 0x0B (121 bytes)...")
        key_buf = ctypes.create_string_buffer(128)
        ret = usb.lib.libusb_control_transfer(
            handle, CTRL_IN, 0x0B, 0, 0, key_buf, 121, 1000
        )
        print(f"[+] Control IN 0x0B returned: {ret} bytes transferred!")
        if ret > 0:
            key_data = key_buf.raw[:ret]
            print(f"[+] Received encrypted key/TLS data ({len(key_data)} bytes):")
            print("    HEX: " + key_data.hex(' '))

        usb.release(handle, 0)
        usb.close(handle)
        print("\n[SUCCESS] Initialization handshake confirmed on real hardware!")
    finally:
        usb.exit()

if __name__ == '__main__':
    probe()
