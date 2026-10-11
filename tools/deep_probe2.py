#!/usr/bin/env python3
"""
tools/deep_probe2.py - Focused diagnostic for FPC 10a5:e340 finger detection.

Fixes from deep_probe.py:
 - Re-opens the device between each test to recover from disconnects.
 - Disables autosuspend at the start.
 - Properly tests the TLS handshake hypothesis with a fresh connection.
 - Tests using libfpcbep.so directly to call fpc_tee_begin_enroll().

Run:  sudo python3 tools/deep_probe2.py
(needs root to write power/control, or run via udev rules)
"""

import sys, os, time, struct, ctypes

CTRL_OUT = 0x40
CTRL_IN  = 0xC0
EP_IN    = 0x81

VID, PID = 0x10a5, 0xe340
SYSFS    = "/sys/bus/usb/devices/1-3"

CAP_ID   = struct.pack('<I', 0x0701100F)
SESSION  = struct.pack('<I', 0x0077FF12)  # FPC_SESSIONID_RESERVED


class USB:
    """Minimal libusb wrapper that re-opens cleanly."""
    def __init__(self):
        self.lib = ctypes.CDLL('libusb-1.0.so.0')
        self.lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        self.lib.libusb_control_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
            ctypes.c_uint16, ctypes.c_uint16, ctypes.c_char_p,
            ctypes.c_uint16, ctypes.c_uint]
        self.lib.libusb_control_transfer.restype = ctypes.c_int
        self.lib.libusb_bulk_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_char_p,
            ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
        self.lib.libusb_bulk_transfer.restype = ctypes.c_int
        self.ctx = ctypes.c_void_p()
        self.lib.libusb_init(ctypes.byref(self.ctx))
        self.h = None

    def open(self):
        if self.h:
            try: self.lib.libusb_release_interface(self.h, 0)
            except: pass
            self.lib.libusb_close(self.h)
            self.h = None
        self.h = self.lib.libusb_open_device_with_vid_pid(self.ctx, VID, PID)
        if not self.h:
            raise RuntimeError("Device not found (may have disconnected)")
        if self.lib.libusb_kernel_driver_active(self.h, 0) == 1:
            self.lib.libusb_detach_kernel_driver(self.h, 0)
        r = self.lib.libusb_claim_interface(self.h, 0)
        if r != 0:
            raise RuntimeError(f"claim failed: {r}")

    def ctrl_out(self, req, wval, data=None, timeout=1000):
        buf = ctypes.create_string_buffer(data) if data else None
        ln  = len(data) if data else 0
        return self.lib.libusb_control_transfer(
            self.h, CTRL_OUT, req, wval, 0, buf, ln, timeout)

    def ctrl_in(self, req, wval, length, timeout=1000):
        buf = ctypes.create_string_buffer(length)
        ret = self.lib.libusb_control_transfer(
            self.h, CTRL_IN, req, wval, 0, buf, length, timeout)
        return ret, buf.raw[:max(0, ret)]

    def bulk_in(self, size=2048, timeout=500):
        buf = ctypes.create_string_buffer(size)
        xfr = ctypes.c_int(0)
        ret = self.lib.libusb_bulk_transfer(
            self.h, EP_IN, buf, size, ctypes.byref(xfr), timeout)
        return ret, xfr.value, buf.raw[:xfr.value]

    def close(self):
        if self.h:
            try: self.lib.libusb_release_interface(self.h, 0)
            except: pass
            self.lib.libusb_close(self.h)
            self.h = None

    def __del__(self):
        self.close()
        self.lib.libusb_exit(self.ctx)


def disable_autosuspend():
    """Write 'on' to power/control to prevent USB selective suspend."""
    path = os.path.join(SYSFS, "power/control")
    try:
        with open(path, 'w') as f:
            f.write('on')
        with open(path) as f:
            val = f.read().strip()
        print(f"  power/control set to: {val}")
    except (PermissionError, OSError) as e:
        print(f"  ⚠️  Cannot write {path}: {e}")
        print(f"  ⚠️  Run: echo on | sudo tee {path}")
    except FileNotFoundError:
        print(f"  ⚠️  {path} not found")

    # Also check status
    for name in ["power/runtime_status", "power/autosuspend_delay_ms"]:
        p = os.path.join(SYSFS, name)
        try:
            with open(p) as f:
                print(f"  {name}: {f.read().strip()}")
        except:
            pass


def drain(usb, label="drain"):
    """Clear EP 0x81."""
    n = 0
    while True:
        r, x, d = usb.bulk_in(timeout=200)
        if r == 0 and x > 0:
            n += 1
            print(f"  [{label}] drained {x} bytes: {d[:20].hex(' ')}...")
        else:
            break
    return n


def init_sensor(usb, session_id=SESSION):
    """ABORT → INIT → read event → S_STATE S0 → 0x0B."""
    usb.ctrl_out(0x03, 0)                             # ABORT
    time.sleep(0.05)
    usb.ctrl_out(0x01, 1, session_id)                  # INIT
    r, x, data = usb.bulk_in(timeout=2000)             # init event
    if x >= 28:
        cmdid, length, status = struct.unpack('<III', data[:12])
        sensor, hw_id, w, h = struct.unpack('<HHHH', data[12:20])
        fw = data[20:36].split(b'\x00')[0].decode('latin1')
        print(f"  [init] cmdid={cmdid} status={status} HW=0x{hw_id:04x} "
              f"{w}x{h} FW={fw}")
    else:
        print(f"  [init] ⚠️  only {x} bytes from init event (ret={r})")
        return False

    usb.ctrl_out(0x08, 0x10)                           # S_STATE S0
    ret_0b, key = usb.ctrl_in(0x0B, 0, 121)            # sealed TLS key
    print(f"  [init] 0x0B → {ret_0b} bytes")
    return ret_0b > 0


def wait_touch(usb, secs=12, label="wait"):
    print(f"\n  >>> PLACE FINGER ON SENSOR NOW ({secs}s) <<<\n")
    sys.stdout.flush()
    t0 = time.time()
    while time.time() - t0 < secs:
        r, x, d = usb.bulk_in(timeout=500)
        if r == 0 and x > 0:
            print(f"\n  [!!!] {label}: GOT {x} BYTES!")
            print(f"        HEX: {d.hex(' ')}")
            if x >= 12:
                c,l,s = struct.unpack('<III', d[:12])
                print(f"        LE: cmdid=0x{c:04x} len={l} status={s}")
                c,l,s = struct.unpack('>III', d[:12])
                print(f"        BE: cmdid=0x{c:04x} len={l} status={s}")
            return True, d
        elif r == -7:
            sys.stdout.write(".")
            sys.stdout.flush()
        else:
            print(f"\n  [{label}] error {r}")
            return False, b""
    print(f"\n  [{label}] timeout after {secs}s")
    return False, b""


# ─────────────────────────────────────────────────────────────
# Individual tests — each re-opens the device fresh
# ─────────────────────────────────────────────────────────────

def test_standard(usb):
    """T1: Standard ARM (baseline, with autosuspend disabled)."""
    print("\n" + "="*60)
    print("T1: Standard ARM (autosuspend disabled)")
    print("="*60)
    usb.open()
    ok = init_sensor(usb)
    if not ok: return False, b""
    drain(usb, "pre-arm")

    print(f"  [arm] 0x02 wValue=1 cap_id=0x0701100f")
    ret = usb.ctrl_out(0x02, 1, CAP_ID)
    print(f"  [arm] ret={ret}")
    return wait_touch(usb, 15, "T1")


def test_tls_handshake(usb):
    """T2: Attempt TLS handshake on fresh connection, then ARM."""
    print("\n" + "="*60)
    print("T2: Full init + TLS handshake attempt + ARM")
    print("="*60)
    usb.open()
    ok = init_sensor(usb)
    if not ok: return False, b""

    # Try TLS handshake
    print("  [tls] Sending 0x05 wValue=1 (TLS handshake start)")
    ret = usb.ctrl_out(0x05, 1, timeout=2000)
    print(f"  [tls] 0x05 ret={ret}")

    if ret >= 0:
        # Try reading TLS server hello
        r, x, d = usb.bulk_in(timeout=3000)
        print(f"  [tls] bulk read: ret={r}, {x} bytes")
        if x > 0:
            print(f"  [tls] data: {d[:60].hex(' ')}")
            if x >= 12:
                c,l,s = struct.unpack('<III', d[:12])
                print(f"  [tls] LE: cmdid=0x{c:04x} len={l} status={s}")
    else:
        print(f"  [tls] ⚠️  0x05 failed (ret={ret}), sensor may not support TLS")

    drain(usb, "post-tls")

    print(f"  [arm] 0x02 wValue=1")
    ret = usb.ctrl_out(0x02, 1, CAP_ID)
    print(f"  [arm] ret={ret}")
    if ret < 0:
        print(f"  [arm] ⚠️  ARM failed after TLS attempt")
        return False, b""
    return wait_touch(usb, 12, "T2")


def test_direct_get_img(usb):
    """T3: ARM + immediate GET_IMG (no finger wait) — does sensor have image data?"""
    print("\n" + "="*60)
    print("T3: ARM + immediate GET_IMG (skip finger-wait)")
    print("="*60)
    usb.open()
    ok = init_sensor(usb)
    if not ok: return False, b""
    drain(usb, "pre")

    print("  [arm] ARM")
    usb.ctrl_out(0x02, 1, CAP_ID)
    time.sleep(0.5)

    print("  [img] GET_IMG (0x09)")
    ret = usb.ctrl_out(0x09, 0)
    print(f"  [img] ret={ret}")
    if ret < 0:
        print(f"  [img] GET_IMG failed")
        return False, b""

    total = 0
    for i in range(10):
        r, x, d = usb.bulk_in(timeout=1000)
        if x > 0:
            total += x
            if i < 3:
                print(f"  [img] chunk {i}: {x} bytes: {d[:40].hex(' ')}")
        else:
            break
    print(f"  [img] total: {total} bytes")
    return total > 0, b""


def test_calibrate_arm(usb):
    """T4: Dead-pixel calibration (0x0A) → ARM → wait."""
    print("\n" + "="*60)
    print("T4: Calibrate (0x0A) → ARM → wait")
    print("="*60)
    usb.open()
    ok = init_sensor(usb)
    if not ok: return False, b""

    print("  [cal] Sending 0x0A")
    ret = usb.ctrl_out(0x0A, 0, timeout=2000)
    print(f"  [cal] ret={ret}")

    if ret >= 0:
        r, x, d = usb.bulk_in(timeout=2000)
        if x > 0:
            print(f"  [cal] response: {x} bytes: {d[:40].hex(' ')}")

    drain(usb, "post-cal")

    print(f"  [arm] ARM")
    ret = usb.ctrl_out(0x02, 1, CAP_ID)
    print(f"  [arm] ret={ret}")
    if ret < 0: return False, b""
    return wait_touch(usb, 10, "T4")


def test_sx_cycle_arm(usb):
    """T5: S_STATE cycle Sx → re-init → ARM → wait."""
    print("\n" + "="*60)
    print("T5: Sx power cycle → re-init → ARM → wait")
    print("="*60)
    usb.open()
    ok = init_sensor(usb)
    if not ok: return False, b""

    print("  [pwr] Sending Sx (0x11)")
    usb.ctrl_out(0x08, 0x11)
    time.sleep(0.5)

    print("  [pwr] Re-init after Sx...")
    ok = init_sensor(usb, struct.pack('<I', 0xDEADBEEF))
    if not ok: return False, b""

    drain(usb, "post-reinit")

    ret = usb.ctrl_out(0x02, 1, CAP_ID)
    print(f"  [arm] ret={ret}")
    if ret < 0: return False, b""
    return wait_touch(usb, 10, "T5")


def test_arm_no_captureid(usb):
    """T6: ARM wValue=1 with empty (zero-length) payload — maybe capture_id is not needed."""
    print("\n" + "="*60)
    print("T6: ARM wValue=1, zero-length payload (no capture_id)")
    print("="*60)
    usb.open()
    ok = init_sensor(usb)
    if not ok: return False, b""
    drain(usb, "pre-arm")

    print("  [arm] 0x02 wValue=1, wLength=0")
    # Send control transfer with no data at all
    ret = usb.lib.libusb_control_transfer(
        usb.h, CTRL_OUT, 0x02, 1, 0, None, 0, 1000)
    print(f"  [arm] ret={ret}")
    if ret < 0:
        print(f"  [arm] failed")
        return False, b""
    return wait_touch(usb, 10, "T6")


def test_abort_arm_sequence(usb):
    """T7: ABORT(wValue=1) → ARM — the ABORT wValue might matter."""
    print("\n" + "="*60)
    print("T7: ABORT(wValue=1) → ARM → wait")
    print("="*60)
    usb.open()
    ok = init_sensor(usb)
    if not ok: return False, b""

    # Try ABORT with wValue=1 instead of 0
    print("  [abort] 0x03 wValue=1")
    ret = usb.ctrl_out(0x03, 1)
    print(f"  [abort] ret={ret}")
    time.sleep(0.1)

    drain(usb, "post-abort")

    ret = usb.ctrl_out(0x02, 1, CAP_ID)
    print(f"  [arm] ret={ret}")
    if ret < 0: return False, b""
    return wait_touch(usb, 10, "T7")


def test_with_libfpcbep(usb):
    """T8: Use libfpcbep.so directly to replicate the full driver init
    including fpc_tee_begin_enroll(), then ARM."""
    print("\n" + "="*60)
    print("T8: Full init via libfpcbep.so + fpc_tee_begin_enroll + ARM")
    print("="*60)

    # Load the blob
    blob_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "blob", "libfpcbep.so")
    if not os.path.exists(blob_path):
        blob_path = "/home/noywrit/FPCSensorController-acerswiftgo14sfg14-41/blob/libfpcbep.so"

    try:
        bep = ctypes.CDLL(blob_path)
    except OSError as e:
        print(f"  ⚠️  Cannot load libfpcbep.so: {e}")
        return False, b""

    usb.open()
    ok = init_sensor(usb)
    if not ok: return False, b""

    # 1. Create enclave
    bep.fpc_create_enclave.restype = ctypes.c_void_p
    enclave = bep.fpc_create_enclave()
    print(f"  [bep] enclave={enclave:#x}" if enclave else "  [bep] enclave=NULL ⚠️")

    if not enclave:
        return False, b""

    # 2. Start enclave
    ret = bep.fpc_start_enclave(ctypes.c_void_p(enclave))
    print(f"  [bep] fpc_start_enclave → {ret}")

    # 3. Init enclave with hwid 0x0331
    bep.fpc_enclave_init.argtypes = [ctypes.c_void_p, ctypes.c_uint16]
    ret = bep.fpc_enclave_init(ctypes.c_void_p(enclave), 0x0331)
    print(f"  [bep] fpc_enclave_init(0x0331) → {ret}")

    # 4. Handle TLS connection with the 121-byte key
    ret_0b, key = usb.ctrl_in(0x0B, 0, 121)
    if ret_0b > 0:
        key_buf = ctypes.create_string_buffer(key)
        bep.fpc_enclave_handle_tls_connection.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        ret = bep.fpc_enclave_handle_tls_connection(
            ctypes.c_void_p(enclave), key_buf, ret_0b)
        print(f"  [bep] fpc_enclave_handle_tls_connection → {ret}")
    else:
        print(f"  [bep] ⚠️ couldn't read 0x0B key (already consumed in init)")

    # 5. Init TLS buffers
    bep.fpc_tls_buff_init()
    bep.fpc_tls_write_buff_init()
    print("  [bep] TLS buffers initialized")

    # 6. fpc_tee_init + fpc_tee_bio_init
    bep.fpc_tee_init.restype = ctypes.c_void_p
    tee = bep.fpc_tee_init()
    print(f"  [bep] fpc_tee_init → {tee:#x}" if tee else "  [bep] tee=NULL ⚠️")

    if not tee:
        return False, b""

    bep.fpc_tee_bio_init.restype = ctypes.c_void_p
    bep.fpc_tee_bio_init.argtypes = [ctypes.c_void_p]
    bio = bep.fpc_tee_bio_init(ctypes.c_void_p(tee))
    print(f"  [bep] fpc_tee_bio_init → {bio:#x}" if bio else "  [bep] bio=NULL ⚠️")

    if not bio:
        return False, b""

    # 7. Load empty DB
    bep.fpc_tee_load_empty_db.argtypes = [ctypes.c_void_p]
    ret = bep.fpc_tee_load_empty_db(ctypes.c_void_p(bio))
    print(f"  [bep] fpc_tee_load_empty_db → {ret}")

    # 8. Begin enroll
    bep.fpc_tee_begin_enroll.argtypes = [ctypes.c_void_p]
    ret = bep.fpc_tee_begin_enroll(ctypes.c_void_p(bio))
    print(f"  [bep] fpc_tee_begin_enroll → {ret}")

    # 9. Clear TLS buffers and ARM
    bep.fpc_tls_buff_clear()

    drain(usb, "pre-arm")

    print(f"  [arm] 0x02 wValue=1")
    ret = usb.ctrl_out(0x02, 1, CAP_ID)
    print(f"  [arm] ret={ret}")
    if ret < 0: return False, b""

    return wait_touch(usb, 15, "T8-bep")


def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  FPC 10a5:e340 Deep Probe v2                           ║")
    print("║  Each test re-opens the device fresh                    ║")
    print("╚══════════════════════════════════════════════════════════╝")

    print("\n--- Disabling USB autosuspend ---")
    disable_autosuspend()

    usb = USB()
    results = {}

    tests = [
        ("T1-standard-arm",      test_standard),
        ("T2-tls-handshake",     test_tls_handshake),
        ("T3-direct-get-img",    test_direct_get_img),
        ("T4-calibrate-arm",     test_calibrate_arm),
        ("T5-sx-power-cycle",    test_sx_cycle_arm),
        ("T6-arm-no-captureid",  test_arm_no_captureid),
        ("T7-abort-wval1",       test_abort_arm_sequence),
        ("T8-libfpcbep-full",    test_with_libfpcbep),
    ]

    for name, func in tests:
        try:
            ok, data = func(usb)
            results[name] = "✅ SUCCESS" if ok else "❌ no response"
        except Exception as e:
            results[name] = f"💥 {e}"
        finally:
            usb.close()
            time.sleep(1)  # Let the sensor settle

    print("\n" + "="*60)
    print("RESULTS SUMMARY")
    print("="*60)
    for name, result in results.items():
        print(f"  {name:25s} : {result}")
    print()


if __name__ == '__main__':
    main()
