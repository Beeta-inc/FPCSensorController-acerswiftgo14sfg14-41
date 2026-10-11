#!/usr/bin/env python3
"""
tools/test_bep_standalone.py - Test full initialization through libfpcbep.so
followed by ARM and 25s touch monitoring.
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
FPC_CMD_REFRESH_SENSOR = 0x20
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
    print("=== Standalone libfpcbep + ARM Test (25s) ===")
    
    # 0. Load libfpcbep.so
    try:
        bep = ctypes.CDLL('blob/libfpcbep.so')
        print("[+] libfpcbep.so loaded successfully.")
    except Exception as e:
        print(f"[-] Failed to load libfpcbep.so: {e}")
        return

    usb = LibUSB()
    try:
        handle = usb.open(0x10a5, 0xe340)
        usb.claim(handle, 0)

        buf = ctypes.create_string_buffer(4096)
        trans = ctypes.c_int(0)

        # 1. ABORT (wValue=1)
        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_ABORT, 1, 0, None, 0, 1000)
        time.sleep(0.05)

        # 2. INIT
        session_id = struct.pack('<I', 0x0077FF12)
        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_INIT, 1, 0, session_id, 4, 1000)
        
        # Read init event
        r = usb.lib.libusb_bulk_transfer(handle, EP_IN, buf, 4096, ctypes.byref(trans), 2000)
        if trans.value >= 28:
            hw_id = struct.unpack('<H', buf.raw[14:16])[0]
            print(f"[+] Sensor initialized: HWID=0x{hw_id:04x}, data len={trans.value}")
        else:
            print(f"[-] Init failed, ret={r}, bytes={trans.value}")
            return

        # 3. S0 State (0x10 is S0 Active working state in firmware!)
        r_s0 = usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_INDICATE_S_STATE, 0x10, 0, None, 0, 1000)
        print(f"[+] S_STATE (0x10 = S0 active) sent, ret={r_s0}")

        # 4. Get sealed TLS key (0x0B)
        ret_0b = usb.lib.libusb_control_transfer(handle, CTRL_IN, 0x0B, 0, 0, buf, 121, 1000)
        print(f"[+] 0x0B returned {ret_0b} bytes.")
        key_data = buf.raw[:ret_0b]

        # 5. Enclave setup via libfpcbep.so
        bep.fpc_create_enclave.restype = ctypes.c_void_p
        enclave = bep.fpc_create_enclave()
        print(f"[+] Enclave created: {enclave:#x}")

        bep.fpc_start_enclave.argtypes = [ctypes.c_void_p]
        ret = bep.fpc_start_enclave(ctypes.c_void_p(enclave))
        print(f"[+] fpc_start_enclave: {ret}")

        bep.fpc_enclave_init.argtypes = [ctypes.c_void_p, ctypes.c_uint16]
        ret = bep.fpc_enclave_init(ctypes.c_void_p(enclave), 0x0331)
        print(f"[+] fpc_enclave_init(0x0331): {ret}")

        # 6. Feed the sealed TLS key to the enclave
        bep.fpc_enclave_handle_tls_connection.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32
        ]
        key_buf = ctypes.create_string_buffer(key_data)
        ret = bep.fpc_enclave_handle_tls_connection(
            ctypes.c_void_p(enclave), key_buf, len(key_data)
        )
        print(f"[+] fpc_enclave_handle_tls_connection: {ret}")

        # 7. Init TLS buffers
        bep.fpc_tls_buff_init()
        bep.fpc_tls_write_buff_init()

        # 8. TEE Init & Begin Enroll
        bep.fpc_tee_init.restype = ctypes.c_void_p
        tee = bep.fpc_tee_init()
        print(f"[+] fpc_tee_init: {tee:#x}")

        bep.fpc_tee_bio_init.restype = ctypes.c_void_p
        bep.fpc_tee_bio_init.argtypes = [ctypes.c_void_p]
        bio = bep.fpc_tee_bio_init(ctypes.c_void_p(tee))
        print(f"[+] fpc_tee_bio_init: {bio:#x}")

        bep.fpc_tee_load_empty_db.argtypes = [ctypes.c_void_p]
        ret = bep.fpc_tee_load_empty_db(ctypes.c_void_p(bio))
        print(f"[+] fpc_tee_load_empty_db: {ret}")

        bep.fpc_tee_begin_enroll.argtypes = [ctypes.c_void_p]
        ret = bep.fpc_tee_begin_enroll(ctypes.c_void_p(bio))
        print(f"[+] fpc_tee_begin_enroll: {ret}")

        # Drain any leftover packets on EP 0x81
        while True:
            r = usb.lib.libusb_bulk_transfer(handle, EP_IN, buf, 4096, ctypes.byref(trans), 100)
            if r == 0 and trans.value > 0:
                print(f"[i] Drained {trans.value} bytes: {buf.raw[:trans.value].hex(' ')}")
            else:
                break

        # Re-assert S0 right before ARM
        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_INDICATE_S_STATE, 0x10, 0, None, 0, 1000)

        # 9. Countdown so user gets ready
        print("\n" + "="*60)
        print(">>> PREPARING TO ARM SENSOR IN HOST S0 WORKING MODE! <<<")
        for countdown in range(5, 0, -1):
            print(f">>> GET READY: ARMING IN {countdown}... <<<")
            sys.stdout.flush()
            time.sleep(1.0)
        print("="*60 + "\n")

        # Send ARM (0x02, wValue=1, cap_id)
        cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
        ret = usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_ARM, 1, 0, cap_id, 4, 1000)
        print(f"[+] ARM sent (ret = {ret}).")

        print("\n" + "="*60)
        print(">>> SENSOR IS NOW FULLY ARMED! <<<")
        print(">>> PLACE AND REST YOUR FINGER ON THE POWER BUTTON SURFACE NOW! <<<")
        print(">>> (Do NOT mechanically click the button - just rest your finger flat on top) <<<")
        print(">>> (Holding for 30 seconds - try touching, tapping, or resting finger) <<<")
        print("="*60 + "\n")
        sys.stdout.flush()

        start = time.time()
        last_log = start
        packets = []
        got_finger_down = False
        while time.time() - start < 30:
            r = usb.lib.libusb_bulk_transfer(handle, EP_IN, buf, 4096, ctypes.byref(trans), 200)
            now = time.time()
            if now - last_log >= 2.0 and not got_finger_down:
                last_log = now
                print(f"  [t={now - start:.1f}s] Sensor active - waiting for finger contact...")
                sys.stdout.flush()
            if r == 0 and trans.value > 0:
                data = buf.raw[:trans.value]
                elapsed = time.time() - start
                packets.append(data)
                print(f"\n[!!! EVENT #{len(packets)} RECEIVED at {elapsed:.2f}s ({trans.value} bytes) !!!]")
                print(f"      HEX: {data[:32].hex(' ')}...")
                if trans.value >= 12:
                    c, l, s = struct.unpack('<III', data[:12])
                    print(f"      Decoded LE: cmdid=0x{c:02x} ({c}), length={l}, status={s}")
                    if c == 6 and not got_finger_down:
                        got_finger_down = True
                        print("\n" + "*"*60)
                        print(">>> TOUCH DETECTED (EVT_FINGER_DWN)! <<<")
                        print(">>> Sending CMD_GET_IMG (0x09) to read fingerprint image... <<<")
                        print("*"*60 + "\n")
                        sys.stdout.flush()
                        usb.lib.libusb_control_transfer(handle, CTRL_OUT, FPC_CMD_GET_IMG, 0, 0, None, 0, 1000)

                # Read followup packets
                while True:
                    r2 = usb.lib.libusb_bulk_transfer(handle, EP_IN, buf, 4096, ctypes.byref(trans), 500)
                    if r2 == 0 and trans.value > 0:
                        more = buf.raw[:trans.value]
                        packets.append(more)
                        print(f"      Followup packet ({trans.value} bytes): {more[:16].hex(' ')}...")
                        if trans.value >= 12:
                            c2, l2, s2 = struct.unpack('<III', more[:12])
                            print(f"      Followup Decoded LE: cmdid=0x{c2:02x} ({c2}), length={l2}, status={s2}")
                    else:
                        break
                if got_finger_down:
                    break
            elif r == -7:
                sys.stdout.write(".")
                sys.stdout.flush()
            else:
                print(f"\n[-] Bulk error: {r}")
                break

        if not packets:
            print("\n[-] No touch events received within 30 seconds.")
        else:
            print(f"\n[+] Total packets captured: {len(packets)}")

        usb.release(handle, 0)
        usb.close(handle)
        print("=== Test finished ===")
    finally:
        usb.exit()

if __name__ == '__main__':
    main()
