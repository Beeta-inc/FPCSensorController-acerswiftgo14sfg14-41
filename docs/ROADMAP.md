# Reverse Engineering Roadmap: FPC Sensor Controller (10a5:e340)
**Device:** Acer Swift Go 14 (SFG14-41) Fingerprint Reader  
**Chip:** Fingerprint Cards AB (FPC) `10a5:e340`  
**bcdDevice:** 23.27 (`cs23_27` family)  
**Endpoint Layout:**
- EP 0: Control transfers (`bmRequestType 0x40` vendor requests)
- EP 1 IN (`0x81`): Bulk IN, packet size 64 bytes

---

## What We Have Already Proven Live on Hardware
1. **Direct Userspace Access:** The device can be claimed from user space via `libusb` without any kernel driver conflict.
2. **Control Endpoint Verification:** The device actively listens on Endpoint 0.
3. **Confirmed Command Acknowledgment:** Sending `bRequest = 0x03` (`FPC_CMD_ABORT`, `wValue = 0x0000`, `wLength = 0`) returns `0` (Success/ACK).
4. **Watchdog Reset:** If an unsupported or unexpected command times out on EP 0, the firmware watchdog resets the sensor and re-enumerates.

---

## The 4-Phase Strategy

### Phase 1: Capture "Known Good" Traffic (Windows)
Because the sensor firmware expects a very specific wake-up and initialization handshake, the fastest way to get the exact sequence is capturing the official Acer Windows driver traffic:

#### Option A: Linux Host `usbmon` + Windows in QEMU/KVM (Recommended)
You don't even need UsbPcap installed inside Windows. QEMU passes the physical USB device to the Windows guest, while Linux records every USB packet on the host bus:
```bash
# 1. Enable usbmon kernel module
sudo modprobe usbmon

# 2. Check which bus the sensor is on (e.g. Bus 1)
lsusb | grep 10a5

# 3. Capture with Wireshark on Linux
sudo wireshark
# Select interface: usbmon1
# Filter: usb.device_address == <device_num> || usb.idVendor == 0x10a5
```

#### Option B: Bare-Metal Windows / Dual-Boot with Wireshark + UsbPcap
If Windows is installed on the machine:
1. Install Wireshark + UsbPcap.
2. Select the UsbPcap interface attached to the FPC root hub.
3. Record distinct sessions:
   - **`01_device_init.pcapng`**: Plug-in / boot / driver load.
   - **`02_finger_present_success.pcapng`**: Enrolled finger unlock.
   - **`03_finger_present_fail.pcapng`**: Unenrolled finger touch.
   - **`04_enroll_step.pcapng`**: Windows Hello enrollment.

---

### Phase 2: Analyze Packet Structure & Crypto
Inspect the captures to answer three fundamental questions:
1. **The Wakeup Sequence:** What exact control transfers (`bRequest`, `wValue`, `wIndex`, payload) are sent before the sensor starts sending bulk data?
2. **Encryption:** Does the sensor initiate a TLS 1.2 PSK session (`Disum PSK`, cipher `0x00AE`), or does it return unencrypted frames (`fpcmoc`)?
3. **Bulk Packet Format:** Does each frame on `0x81` start with the 12-byte header:
   ```c
   struct evt_hdr {
       uint32_t cmdid;
       uint32_t length;
       uint32_t status;
   };
   ```

---

### Phase 3: Build the User-Space Prototype
Implement the working protocol in `tools/probe.py`:
- Replay the initialization sequence.
- Wait for finger-down interrupt or poll bulk endpoint `0x81`.
- Capture raw image buffers or authentication tokens.

---

### Phase 4: Driver Integration
Depending on whether the sensor is Match-on-Chip (MoC) or Match-on-Host (MoH):
- **If Match-on-Chip (MoC):** Add `0xE340` to upstream `libfprint/drivers/fpcmoc/fpc.c`.
- **If Match-on-Host (MoH):** Integrate with `fpcmoh` using `libfpcbep.so` and endpoint `0x81` mapping.
