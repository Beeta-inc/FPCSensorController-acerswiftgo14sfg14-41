#!/usr/bin/env python3
"""
tools/deep_probe.py - Deep diagnostic probe for FPC 10a5:e340 finger detection.

Tests multiple hypotheses about why EP 0x81 does not emit a finger-down event
after ARM (0x02):

  Hypothesis 1: The sensor needs the TLS handshake to complete before it will
                 respond to ARM with finger events. The current patch skips TLS.
  Hypothesis 2: The capture_id value matters - try different values.
  Hypothesis 3: A calibration command (0x0A, 0x0C) must precede ARM.
  Hypothesis 4: The sensor needs EP 0x81 drained of stale data first.
  Hypothesis 5: The ARM timeout is the problem (sensor ACKs but no event follows).
  Hypothesis 6: Power management is interfering (autosuspend, persist).

Run: python3 tools/deep_probe.py
"""

import sys
import time
import struct
import ctypes
import os

LIBUSB_ENDPOINT_IN  = 0x80
LIBUSB_ENDPOINT_OUT = 0x00
LIBUSB_REQUEST_TYPE_VENDOR = (0x02 << 5)
LIBUSB_RECIPIENT_DEVICE    = 0x00

CTRL_OUT = LIBUSB_ENDPOINT_OUT | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE  # 0x40
CTRL_IN  = LIBUSB_ENDPOINT_IN  | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE  # 0xC0

FPC_CMD_INIT             = 0x01
FPC_CMD_ARM              = 0x02
FPC_CMD_ABORT            = 0x03
FPC_CMD_TLS_HANDSHAKE    = 0x05
FPC_CMD_TLS_DATA         = 0x06
FPC_CMD_INDICATE_S_STATE = 0x08
FPC_CMD_GET_IMG          = 0x09
FPC_CMD_DEAD_PIXELS      = 0x0A
FPC_CMD_GET_KPI          = 0x0C

FPC_HOST_MS_S0           = 0x10
FPC_HOST_MS_SX           = 0x11
FPC_CAPTUREID_RESERVED   = 0x0701100F
EP_IN = 0x81


class LibUSB:
    """Thin ctypes wrapper around libusb-1.0."""
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
        h = self.lib.libusb_open_device_with_vid_pid(self.ctx, vid, pid)
        if not h:
            raise RuntimeError(f"Device {vid:04x}:{pid:04x} not found.")
        return h

    def claim(self, h, iface=0):
        if self.lib.libusb_kernel_driver_active(h, iface) == 1:
            self.lib.libusb_detach_kernel_driver(h, iface)
        r = self.lib.libusb_claim_interface(h, iface)
        if r != 0:
            raise RuntimeError(f"claim_interface failed: {r}")

    def release(self, h, iface=0):
        self.lib.libusb_release_interface(h, iface)

    def close(self, h):
        self.lib.libusb_close(h)

    def exit(self):
        self.lib.libusb_exit(self.ctx)


def ctrl_out(usb, h, req, wval, data=None, wlen=0, timeout=1000):
    buf = ctypes.create_string_buffer(data) if data else None
    ln  = len(data) if data else wlen
    return usb.lib.libusb_control_transfer(h, CTRL_OUT, req, wval, 0, buf, ln, timeout)


def ctrl_in(usb, h, req, wval, length, timeout=1000):
    buf = ctypes.create_string_buffer(length)
    ret = usb.lib.libusb_control_transfer(h, CTRL_IN, req, wval, 0, buf, length, timeout)
    return ret, buf.raw[:max(0, ret)]


def bulk_read(usb, h, size=2048, timeout=500):
    buf = ctypes.create_string_buffer(size)
    transferred = ctypes.c_int(0)
    ret = usb.lib.libusb_bulk_transfer(h, EP_IN, buf, size, ctypes.byref(transferred), timeout)
    return ret, transferred.value, buf.raw[:transferred.value]


def drain_ep(usb, h, label="drain"):
    """Read all pending data from EP 0x81 until timeout."""
    count = 0
    while True:
        ret, xfr, data = bulk_read(usb, h, timeout=200)
        if ret == 0 and xfr > 0:
            count += 1
            print(f"  [{label}] drained {xfr} bytes: {data[:20].hex(' ')}...")
        else:
            break
    if count:
        print(f"  [{label}] drained {count} stale packet(s)")
    else:
        print(f"  [{label}] EP 0x81 was clean (no stale data)")
    return count


def do_init_sequence(usb, h, session_id=0x12345678):
    """Standard init: ABORT → INIT → read event → S_STATE → 0x0B."""
    # 1. ABORT
    ctrl_out(usb, h, FPC_CMD_ABORT, 0)
    # 2. INIT
    sid = struct.pack('<I', session_id)
    ctrl_out(usb, h, FPC_CMD_INIT, 1, sid)
    # 3. Read init event from bulk
    ret, xfr, data = bulk_read(usb, h, timeout=1000)
    if xfr >= 28:
        cmdid, length, status = struct.unpack('<III', data[:12])
        sensor, hw_id, w, h_dim = struct.unpack('<HHHH', data[12:20])
        fw_ver = data[20:36].split(b'\x00')[0].decode('latin1')
        print(f"  [init] cmdid={cmdid}, status={status}, sensor={sensor}, "
              f"HWID=0x{hw_id:04x}, {w}x{h_dim}, FW={fw_ver}")
    else:
        print(f"  [init] WARNING: only got {xfr} bytes from init event")
    # 4. S_STATE S0
    ctrl_out(usb, h, FPC_CMD_INDICATE_S_STATE, FPC_HOST_MS_S0)
    # 5. Read 0x0B (sealed TLS key)
    ret_0b, key_data = ctrl_in(usb, h, 0x0B, 0, 121)
    print(f"  [init] 0x0B returned {ret_0b} bytes")
    return key_data


def wait_for_touch(usb, h, timeout_sec=15, label="touch-wait"):
    """Block on EP 0x81 for up to timeout_sec, printing dots every 500ms."""
    print(f"\n  >>> PLACE YOUR FINGER ON THE SENSOR NOW ({timeout_sec}s window) <<<\n")
    sys.stdout.flush()
    start = time.time()
    while time.time() - start < timeout_sec:
        ret, xfr, data = bulk_read(usb, h, timeout=500)
        if ret == 0 and xfr > 0:
            print(f"\n  [!!!] {label}: GOT {xfr} BYTES on EP 0x81!")
            print(f"        HEX: {data.hex(' ')}")
            if xfr >= 12:
                c, l, s = struct.unpack('<III', data[:12])
                print(f"        LE decode: cmdid=0x{c:02x} len={l} status={s}")
                c, l, s = struct.unpack('>III', data[:12])
                print(f"        BE decode: cmdid=0x{c:02x} len={l} status={s}")
            return True, data
        elif ret == -7:  # LIBUSB_ERROR_TIMEOUT
            sys.stdout.write(".")
            sys.stdout.flush()
        else:
            print(f"\n  [{label}] bulk error: {ret}")
            return False, b""
    print(f"\n  [{label}] no data after {timeout_sec}s")
    return False, b""


def check_power(bus_dev_path="/sys/bus/usb/devices/1-3"):
    """Check power management settings."""
    print("\n--- Power Management Check ---")
    for f in ["power/control", "power/runtime_status", "power/autosuspend_delay_ms", "power/persist"]:
        path = os.path.join(bus_dev_path, f)
        try:
            with open(path) as fh:
                val = fh.read().strip()
            mark = " ⚠️" if (f == "power/control" and val == "auto") else ""
            print(f"  {f}: {val}{mark}")
        except (FileNotFoundError, PermissionError) as e:
            print(f"  {f}: {e}")


def test_standard_arm(usb, h):
    """Test 1: Standard ARM (0x02, wValue=1, capture_id=0x0701100F) then wait."""
    print("\n" + "="*60)
    print("TEST 1: Standard ARM → wait for finger-down event")
    print("="*60)
    do_init_sequence(usb, h)
    drain_ep(usb, h, "pre-arm drain")

    cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
    print(f"  [arm] Sending 0x02, wValue=1, cap_id=0x{FPC_CAPTUREID_RESERVED:08x}")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    print(f"  [arm] ret={ret}")

    return wait_for_touch(usb, h, timeout_sec=15, label="T1")


def test_arm_with_different_captureid(usb, h):
    """Test 2: Try ARM with capture_id=0 (some sensors use plain zero)."""
    print("\n" + "="*60)
    print("TEST 2: ARM with capture_id=0x00000000")
    print("="*60)
    do_init_sequence(usb, h)
    drain_ep(usb, h, "pre-arm drain")

    cap_id = struct.pack('<I', 0x00000000)
    print("  [arm] Sending 0x02, wValue=1, cap_id=0x00000000")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    print(f"  [arm] ret={ret}")

    return wait_for_touch(usb, h, timeout_sec=10, label="T2")


def test_arm_wvalue0(usb, h):
    """Test 3: ARM with wValue=0 (some firmware branches differently)."""
    print("\n" + "="*60)
    print("TEST 3: ARM with wValue=0, no payload")
    print("="*60)
    do_init_sequence(usb, h)
    drain_ep(usb, h, "pre-arm drain")

    print("  [arm] Sending 0x02, wValue=0, no payload")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 0, timeout=2000)
    print(f"  [arm] ret={ret}")

    return wait_for_touch(usb, h, timeout_sec=10, label="T3")


def test_calibrate_then_arm(usb, h):
    """Test 4: Send dead-pixel/calibration (0x0A) before ARM."""
    print("\n" + "="*60)
    print("TEST 4: Calibrate (0x0A) → ARM → wait")
    print("="*60)
    do_init_sequence(usb, h)
    drain_ep(usb, h, "pre-cal drain")

    print("  [cal] Sending 0x0A, wValue=0")
    ret = ctrl_out(usb, h, FPC_CMD_DEAD_PIXELS, 0)
    print(f"  [cal] 0x0A ret={ret}")

    # Read any calibration response
    ret_b, xfr_b, data_b = bulk_read(usb, h, timeout=1000)
    if xfr_b > 0:
        print(f"  [cal] bulk response: {xfr_b} bytes: {data_b[:40].hex(' ')}")

    cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
    print(f"  [arm] Sending 0x02, wValue=1, cap_id=0x{FPC_CAPTUREID_RESERVED:08x}")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    print(f"  [arm] ret={ret}")

    return wait_for_touch(usb, h, timeout_sec=10, label="T4")


def test_kpi_then_arm(usb, h):
    """Test 5: GET_KPI (0x0C) → ARM → wait."""
    print("\n" + "="*60)
    print("TEST 5: GET_KPI (0x0C) → ARM → wait")
    print("="*60)
    do_init_sequence(usb, h)
    drain_ep(usb, h, "pre-kpi drain")

    print("  [kpi] Sending 0x0C, wValue=0")
    ret = ctrl_out(usb, h, FPC_CMD_GET_KPI, 0)
    print(f"  [kpi] 0x0C ret={ret}")

    ret_b, xfr_b, data_b = bulk_read(usb, h, timeout=1000)
    if xfr_b > 0:
        print(f"  [kpi] bulk response: {xfr_b} bytes: {data_b[:40].hex(' ')}")

    cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
    print(f"  [arm] Sending 0x02, wValue=1")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    print(f"  [arm] ret={ret}")

    return wait_for_touch(usb, h, timeout_sec=10, label="T5")


def test_sx_s0_cycle_then_arm(usb, h):
    """Test 6: Cycle S_STATE Sx→S0 to simulate a wake-from-sleep, then ARM."""
    print("\n" + "="*60)
    print("TEST 6: Power cycle (Sx → S0) → re-init → ARM → wait")
    print("="*60)
    do_init_sequence(usb, h)

    # Indicate Sx (sleep)
    print("  [pwr] Sending S_STATE Sx (0x11)")
    ctrl_out(usb, h, FPC_CMD_INDICATE_S_STATE, FPC_HOST_MS_SX)
    time.sleep(0.5)

    # Now re-init
    print("  [pwr] Re-initializing after Sx...")
    do_init_sequence(usb, h, session_id=0xDEADBEEF)
    drain_ep(usb, h, "post-reinit drain")

    cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
    print(f"  [arm] Sending 0x02, wValue=1")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    print(f"  [arm] ret={ret}")

    return wait_for_touch(usb, h, timeout_sec=10, label="T6")


def test_tls_handshake_then_arm(usb, h):
    """Test 7: Attempt TLS handshake (0x05 wValue=1) even though the e340 skips it,
    then ARM. Maybe the sensor expects this."""
    print("\n" + "="*60)
    print("TEST 7: Attempt TLS handshake (0x05) → ARM → wait")
    print("="*60)
    do_init_sequence(usb, h)

    print("  [tls] Sending 0x05 wValue=1")
    ret = ctrl_out(usb, h, FPC_CMD_TLS_HANDSHAKE, 1)
    print(f"  [tls] 0x05 ret={ret}")

    # Try reading TLS hello
    ret_b, xfr_b, data_b = bulk_read(usb, h, timeout=2000)
    print(f"  [tls] bulk after 0x05: ret={ret_b}, {xfr_b} bytes")
    if xfr_b > 0:
        print(f"  [tls] data: {data_b[:60].hex(' ')}")

    drain_ep(usb, h, "post-tls drain")

    cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
    print(f"  [arm] Sending 0x02, wValue=1")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    print(f"  [arm] ret={ret}")

    return wait_for_touch(usb, h, timeout_sec=10, label="T7")


def test_double_arm(usb, h):
    """Test 8: Send ARM twice — some sensors need the first ARM to 'prime'
    the capacitive threshold and the second to actually listen."""
    print("\n" + "="*60)
    print("TEST 8: Double ARM → wait")
    print("="*60)
    do_init_sequence(usb, h)
    drain_ep(usb, h, "pre-arm drain")

    cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
    print("  [arm1] Sending first ARM")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    print(f"  [arm1] ret={ret}")
    time.sleep(0.1)

    # Read any immediate response
    ret_b, xfr_b, data_b = bulk_read(usb, h, timeout=200)
    if xfr_b > 0:
        print(f"  [arm1] immediate bulk: {xfr_b}b: {data_b.hex(' ')}")

    # ABORT then re-ARM
    ctrl_out(usb, h, FPC_CMD_ABORT, 0)
    time.sleep(0.1)
    drain_ep(usb, h, "between-arms drain")

    print("  [arm2] Sending second ARM")
    ret = ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    print(f"  [arm2] ret={ret}")

    return wait_for_touch(usb, h, timeout_sec=10, label="T8")


def test_get_img_directly(usb, h):
    """Test 9: Skip finger-down entirely — just send GET_IMG (0x09)
    and see if the sensor returns anything (image data or error)."""
    print("\n" + "="*60)
    print("TEST 9: Direct GET_IMG (0x09) without waiting for finger")
    print("="*60)
    do_init_sequence(usb, h)
    drain_ep(usb, h, "pre-img drain")

    cap_id = struct.pack('<I', FPC_CAPTUREID_RESERVED)
    print("  [arm] ARM first")
    ctrl_out(usb, h, FPC_CMD_ARM, 1, cap_id)
    time.sleep(0.2)

    print("  [img] GET_IMG (0x09)")
    ret = ctrl_out(usb, h, FPC_CMD_GET_IMG, 0)
    print(f"  [img] ret={ret}")

    # Read bulk
    total = 0
    for i in range(5):
        ret_b, xfr_b, data_b = bulk_read(usb, h, timeout=1000)
        if xfr_b > 0:
            total += xfr_b
            print(f"  [img] chunk {i}: {xfr_b} bytes: {data_b[:40].hex(' ')}")
        else:
            break
    print(f"  [img] total image bytes: {total}")
    return total > 0, b""


def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  FPC 10a5:e340 Deep Diagnostic Probe                   ║")
    print("║  Testing why finger detection is not working            ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # Power check first
    check_power()

    usb = LibUSB()
    try:
        h = usb.open(0x10a5, 0xe340)
        usb.claim(h, 0)
        print("\n[+] Device opened & claimed.\n")

        results = {}

        # Run tests in order. ABORT between each test to reset sensor state.
        tests = [
            ("T1-standard-arm",       test_standard_arm),
            ("T2-captureid-zero",     test_arm_with_different_captureid),
            ("T3-arm-wvalue0",        test_arm_wvalue0),
            ("T4-calibrate-first",    test_calibrate_then_arm),
            ("T5-kpi-first",          test_kpi_then_arm),
            ("T6-power-cycle",        test_sx_s0_cycle_then_arm),
            ("T7-tls-handshake",      test_tls_handshake_then_arm),
            ("T8-double-arm",         test_double_arm),
            ("T9-direct-get-img",     test_get_img_directly),
        ]

        for name, func in tests:
            try:
                ctrl_out(usb, h, FPC_CMD_ABORT, 0)
                time.sleep(0.2)
                ok, data = func(usb, h)
                results[name] = "✅ SUCCESS" if ok else "❌ no response"
            except Exception as e:
                results[name] = f"💥 ERROR: {e}"
                # Try to recover
                try:
                    ctrl_out(usb, h, FPC_CMD_ABORT, 0)
                except:
                    pass

        usb.release(h, 0)
        usb.close(h)

        print("\n" + "="*60)
        print("RESULTS SUMMARY")
        print("="*60)
        for name, result in results.items():
            print(f"  {name:25s} : {result}")

    finally:
        usb.exit()


if __name__ == '__main__':
    main()
