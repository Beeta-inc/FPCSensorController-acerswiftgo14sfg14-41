#!/usr/bin/env python3
"""
tools/probe_sstate.py - Probe which CMD_INDICATE_S_STATE wValues
actually change the firmware's internal power state, and try
to find a path to S0 that enables EVT_FINGER_DWN.
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

    def get_state():
        r = libusb.libusb_control_transfer(h, CTRL_IN, 0x50, 1, 0, buf, 42, 1000)
        if r == 42:
            data = buf.raw[:42]
            return data[38], data  # pwr byte, full data
        return None, None

    def drain_bulk():
        while True:
            r = libusb.libusb_bulk_transfer(h, 0x81, buf, 4096, ctypes.byref(trans), 50)
            if r != 0 or trans.value <= 0:
                break

    # 1. Reset with ABORT + INIT
    libusb.libusb_control_transfer(h, CTRL_OUT, 0x03, 1, 0, None, 0, 1000)
    time.sleep(0.05)
    session_id = struct.pack('<I', 0x0077FF12)
    libusb.libusb_control_transfer(h, CTRL_OUT, 0x01, 1, 0, session_id, 4, 1000)
    libusb.libusb_bulk_transfer(h, 0x81, buf, 4096, ctypes.byref(trans), 2000)
    print(f"[+] Init done, resp={trans.value} bytes")
    
    pwr0, _ = get_state()
    print(f"[+] Initial power state: 0x{pwr0:02x}")

    # 2. Test ALL possible wValues for CMD_INDICATE_S_STATE (0x08)
    print("\n=== Testing all CMD_INDICATE_S_STATE wValues ===")
    interesting = []
    for wval in range(0x00, 0x20):
        # Reset to known state first with ABORT
        libusb.libusb_control_transfer(h, CTRL_OUT, 0x03, 1, 0, None, 0, 1000)
        time.sleep(0.02)
        libusb.libusb_control_transfer(h, CTRL_OUT, 0x01, 1, 0, session_id, 4, 1000)
        drain_bulk()
        
        # Send S_STATE with test wValue
        ret = libusb.libusb_control_transfer(h, CTRL_OUT, 0x08, wval, 0, None, 0, 1000)
        time.sleep(0.02)
        
        pwr, raw = get_state()
        if pwr is not None:
            changed = "<<<< CHANGED!" if pwr != pwr0 else ""
            if pwr != pwr0 or wval in [0x10, 0x11, 0x13, 0x14]:
                print(f"  wValue=0x{wval:02x}: ret={ret}, pwr=0x{pwr:02x} {changed}")
                interesting.append((wval, pwr, ret))
        else:
            print(f"  wValue=0x{wval:02x}: ret={ret}, GET_STATE failed")

    # 3. Also try with data in the control transfer (some firmware variants expect data)
    print("\n=== Testing S_STATE with data payload ===")
    for wval in [0x00, 0x01, 0x10]:
        libusb.libusb_control_transfer(h, CTRL_OUT, 0x03, 1, 0, None, 0, 1000)
        time.sleep(0.02)
        libusb.libusb_control_transfer(h, CTRL_OUT, 0x01, 1, 0, session_id, 4, 1000)
        drain_bulk()
        
        payload = struct.pack('<H', wval)
        ret = libusb.libusb_control_transfer(h, CTRL_OUT, 0x08, wval, 0, payload, 2, 1000)
        time.sleep(0.02)
        pwr, _ = get_state()
        if pwr is not None:
            changed = "<<<< CHANGED!" if pwr != pwr0 else ""
            print(f"  wValue=0x{wval:02x} +data: ret={ret}, pwr=0x{pwr:02x} {changed}")

    # 4. Try CMD_TLS_INIT (0x05) first, then S_STATE
    print("\n=== Testing S_STATE after CMD_TLS_INIT ===")
    libusb.libusb_control_transfer(h, CTRL_OUT, 0x03, 1, 0, None, 0, 1000)
    time.sleep(0.02)
    libusb.libusb_control_transfer(h, CTRL_OUT, 0x01, 1, 0, session_id, 4, 1000)
    drain_bulk()
    
    # Get TLS key first
    ret_0b = libusb.libusb_control_transfer(h, CTRL_IN, 0x0B, 0, 0, buf, 121, 1000)
    print(f"  TLS key ret={ret_0b}")
    
    # Send TLS_INIT
    ret_05 = libusb.libusb_control_transfer(h, CTRL_OUT, 0x05, 1, 0, None, 0, 1000)
    print(f"  CMD_TLS_INIT ret={ret_05}")
    time.sleep(0.1)
    drain_bulk()
    
    pwr_after_tls, raw_after_tls = get_state()
    print(f"  After TLS_INIT: pwr=0x{pwr_after_tls:02x if pwr_after_tls is not None else 0:02x}")
    
    # Now try S_STATE
    ret = libusb.libusb_control_transfer(h, CTRL_OUT, 0x08, 0x10, 0, None, 0, 1000)
    time.sleep(0.02)
    pwr, _ = get_state()
    if pwr is not None:
        changed = "<<<< CHANGED!" if pwr != pwr0 else ""
        print(f"  After TLS_INIT + S_STATE(0x10): pwr=0x{pwr:02x} {changed}")

    # 5. Dump full GET_STATE response for analysis
    print("\n=== Full GET_STATE dump ===")
    pwr, raw = get_state()
    if raw:
        for i in range(0, len(raw), 16):
            hex_part = ' '.join(f'{b:02x}' for b in raw[i:i+16])
            print(f"  [{i:3d}] {hex_part}")

    # 6. Try reading the valid states table from flash via CMD_GET_FW_VERSION or debug commands
    print("\n=== Trying CMD_GET_FW_VERSION (request 0x0F) ===")
    ret = libusb.libusb_control_transfer(h, CTRL_IN, 0x0F, 0, 0, buf, 256, 1000)
    if ret > 0:
        print(f"  FW version ({ret} bytes): {buf.raw[:ret].hex(' ')}")
    else:
        print(f"  ret={ret}")

    # Try GET_HW_UNIQUE_ID
    print("\n=== Trying CMD_GET_HW_UNIQUE_ID (request 0x14) ===")
    ret = libusb.libusb_control_transfer(h, CTRL_IN, 0x14, 0, 0, buf, 256, 1000)
    if ret > 0:
        print(f"  HW unique ID ({ret} bytes): {buf.raw[:ret].hex(' ')}")
    else:
        print(f"  ret={ret}")

    libusb.libusb_release_interface(h, 0)
    libusb.libusb_close(h)
    libusb.libusb_exit(ctx)

if __name__ == '__main__':
    main()
