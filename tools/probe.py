#!/usr/bin/env python3
"""
tools/probe.py - Non-destructive probe for FPC Sensor Controller (10a5:e340).
Uses libusb-1.0 directly via ctypes.
"""

import sys
import time
import struct
import ctypes

LIBUSB_ENDPOINT_IN = 0x80
LIBUSB_REQUEST_TYPE_VENDOR = (0x02 << 5)
LIBUSB_RECIPIENT_DEVICE = 0x00
LIBUSB_ENDPOINT_OUT = 0x00

# bmRequestType
# Host to Device, Vendor, Device = 0x40
CTRL_OUT = LIBUSB_ENDPOINT_OUT | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE
# Device to Host, Vendor, Device = 0xC0
CTRL_IN  = LIBUSB_ENDPOINT_IN  | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE

FPC_CMD_INIT = 0x01
FPC_CMD_ARM = 0x02
FPC_CMD_ABORT = 0x03

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
        # Detach kernel driver if active
        if self.lib.libusb_kernel_driver_active(handle, interface) == 1:
            print(f"Detaching kernel driver from interface {interface}...")
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
        print("[+] Device 10a5:e340 opened successfully.")
        usb.claim(handle, 0)
        print("[+] Interface 0 claimed.")

        # Step 1: Send FPC_CMD_ABORT (0x03, wValue=0) to reset state
        print(f"\n[*] Sending FPC_CMD_ABORT (bRequest=0x{FPC_CMD_ABORT:02x}, wValue=0x0000)...")
        ctrl_buf = ctypes.create_string_buffer(64)
        ret = usb.lib.libusb_control_transfer(
            handle,
            CTRL_OUT,      # 0x40
            FPC_CMD_ABORT, # 0x03
            0x0000,        # wValue = 0
            0x0000,        # wIndex = 0
            ctrl_buf,
            0,             # wLength = 0
            2000           # timeout 2s
        )
        print(f"[+] ABORT control transfer returned: {ret}")

        # Step 2: Send FPC_CMD_INIT (0x01)
        print(f"\n[*] Sending FPC_CMD_INIT (bRequest=0x{FPC_CMD_INIT:02x}, bmRequestType=0x{CTRL_OUT:02x})...")
        ret = usb.lib.libusb_control_transfer(
            handle,
            CTRL_OUT,      # 0x40
            FPC_CMD_INIT,  # 0x01
            0x0000,        # wValue
            0x0000,        # wIndex
            ctrl_buf,
            0,             # wLength = 0
            2000           # timeout 2s
        )
        print(f"[+] INIT control transfer returned: {ret}")

        # Now read from Bulk IN EP 0x81
        print(f"[*] Reading response from Bulk IN (EP 0x{EP_IN:02x})...")
        bulk_buf = ctypes.create_string_buffer(2048)
        transferred = ctypes.c_int(0)
        ret = usb.lib.libusb_bulk_transfer(
            handle,
            EP_IN,
            bulk_buf,
            2048,
            ctypes.byref(transferred),
            3000           # timeout 3s
        )
        print(f"[+] Bulk transfer returned: {ret} (transferred {transferred.value} bytes)")

        if transferred.value > 0:
            raw = bulk_buf.raw[:transferred.value]
            print(f"[+] Raw response ({len(raw)} bytes):")
            print("    HEX: " + raw.hex(' '))
            print("    ASCII: " + "".join(chr(b) if 32 <= b < 127 else "." for b in raw))

            # Try parsing as FPC event header (Little Endian - fpcmoc)
            if len(raw) >= 12:
                cmdid_le, length_le, status_le = struct.unpack('<III', raw[:12])
                print(f"\n--- Little-Endian Parse (fpcmoc / MoC) ---")
                print(f"  cmdid:  {cmdid_le} (0x{cmdid_le:04x})")
                print(f"  length: {length_le}")
                print(f"  status: {status_le}")

                if cmdid_le == 2 and len(raw) >= 28: # FPC_EVT_INIT_RESULT
                    sensor, hw_id, w, h = struct.unpack('<HHHH', raw[12:20])
                    fw_ver = raw[20:36].split(b'\x00')[0].decode('latin1', errors='replace')
                    print(f"  --> Sensor Type: {sensor}")
                    print(f"  --> HW ID:       0x{hw_id:04x}")
                    print(f"  --> Resolution:  {w} x {h}")
                    print(f"  --> Firmware:    {fw_ver}")

                # Try parsing as FPC event header (Big Endian - fpcmoh)
                cmdid_be, length_be, status_be = struct.unpack('>III', raw[:12])
                print(f"\n--- Big-Endian Parse (fpcmoh / MoH) ---")
                print(f"  cmdid:  {cmdid_be} (0x{cmdid_be:04x})")
                print(f"  length: {length_be}")
                print(f"  status: {status_be}")
                if cmdid_be == 2 and len(raw) >= 28:
                    sensor, hw_id, w, h = struct.unpack('>HHHH', raw[12:20])
                    fw_ver = raw[20:36].split(b'\x00')[0].decode('latin1', errors='replace')
                    print(f"  --> Sensor Type: {sensor}")
                    print(f"  --> HW ID:       0x{hw_id:04x}")
                    print(f"  --> Resolution:  {w} x {h}")
                    print(f"  --> Firmware:    {fw_ver}")

        usb.release(handle, 0)
        usb.close(handle)
    finally:
        usb.exit()

if __name__ == '__main__':
    probe()
