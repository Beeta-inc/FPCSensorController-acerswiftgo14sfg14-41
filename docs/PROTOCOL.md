# FPC Sensor Controller Protocol Reference

This document summarizes known constants, structures, and behavior for FPC (Fingerprint Cards AB) USB controllers, collated from upstream `libfprint` (`fpcmoc`), the FPC Match-on-Host merge request (`fpcmoh`), and live measurements on `10a5:e340`.

---

## 1. USB Bus Details
* **Vendor ID:** `0x10A5`
* **Product ID:** `0xE340`
* **Interface:** Interface `0`, AltSetting `0`
* **Endpoints:**
  * Control Endpoint `0x00` (Setup transfers)
  * Bulk IN Endpoint `0x81` (Endpoint 1 IN, 64-byte max packet)

---

## 2. Control Transfers (EP 0)
* **Host to Device:** `bmRequestType = 0x40` (Direction: Host-to-Device, Type: Vendor, Recipient: Device)
* **Device to Host:** `bmRequestType = 0xC0` (Direction: Device-to-Host, Type: Vendor, Recipient: Device)

### Known Vendor Requests (`bRequest`)
| `bRequest` | Name | Description |
|---|---|---|
| `0x01` | `FPC_CMD_INIT` | Sensor initialization / wake up |
| `0x02` | `FPC_CMD_ARM` | Arm sensor for finger touch / capture |
| `0x03` | `FPC_CMD_ABORT` | Abort pending capture or reset state machine (`wValue=0`) |
| `0x05`, `0x06` | `FPC_CMD_TLS` | TLS handshake transport (in MoH mode) |
| `0x08` | `FPC_CMD_INDICATE_S_STATE` | System power state indication (`0x10` = S0 active, `0x11` = Sx suspend) |
| `0x09` | `FPC_CMD_GET_IMG` | Image capture transfer trigger |
| `0x0A` | `FPC_CMD_DEAD_PIXELS` | Dead pixel calibration read |
| `0x0C` | `FPC_CMD_GET_KPI` | Performance metrics / KPI |

### High-level MoC Commands (Match-on-Chip mode)
| `bRequest` | Name | Description |
|---|---|---|
| `0x60` | `FPC_CMD_LOAD_DB` | Load biometric database |
| `0x61` | `FPC_CMD_STORE_DB` | Store biometric database |
| `0x62` | `FPC_CMD_DELETE_DB` | Delete biometric database |
| `0x63` | `FPC_CMD_DELETE_TEMPLATE` | Delete enrolled template |
| `0x67` | `FPC_CMD_BEGIN_ENROL` | Begin enrollment process |
| `0x68` | `FPC_CMD_ENROL` | Submit enrollment sample |
| `0x69` | `FPC_CMD_END_ENROL` | Finalize enrollment |
| `0x6A` | `FPC_CMD_BIND_IDENTITY` | Bind template to user identity |
| `0x6B` | `FPC_CMD_IDENTIFY` | Run 1:N fingerprint match |
| `0x70` | `FPC_CMD_ENUM` | Enumerate enrolled IDs |

---

## 3. Bulk Event Framed Records (EP 0x81)

All frames received on the Bulk IN endpoint begin with a 12-byte header:

```c
#pragma pack(push, 1)
struct evt_hdr {
    uint32_t cmdid;     /* Event type ID */
    uint32_t length;    /* Total frame length including this 12-byte header */
    uint32_t status;    /* Status code (0 = success) */
};
#pragma pack(pop)
```

*(Note: In `fpcmoc` the integers are Little-Endian, while in `fpcmoh` they are Big-Endian).*

### Event Types (`cmdid`)
| ID | Constant | Meaning |
|---|---|---|
| `0x02` | `FPC_EVT_INIT_RESULT` | Result of `FPC_CMD_INIT`. Returns HW ID, sensor dimensions, FW version string. |
| `0x05` | `FPC_EVT_TLS` | TLS payload record (in MoH mode). |
| `0x06` | `FPC_EVT_FINGER_DWN` | Finger detected on sensor. |
| `0x08` | `FPC_EVT_IMG` | Image data frame. |
| `0x31` | `FPC_EVT_FID_DATA` | Enrolled template identity data. |
