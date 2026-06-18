# Telemetry Protocol Specification (revision 1.1)

## Frame Format

Each telemetry frame is **4 bytes** long:

| Byte index | Field   | Size | Description                                                                 |
|------------|---------|------|-----------------------------------------------------------------------------|
| 0          | Header  | 1 B  | Fixed value `0xFF` (frame start marker)                                     |
| 1          | State   | 1 B  | Encoded system state with external connection flag (see [State Byte Encoding](#state-byte-encoding)) |
| 2          | Error   | 1 B  | Encoded error code (see [Error Codes](#error-codes))                        |
| 3          | CRC     | 1 B  | CRC-8 calculated over bytes `[1]` and `[2]` (state + error)                 |

- **Frame length:** 4 bytes
- **Transmission interval:** 200 ms

---

## USART Configuration

Telemetry is transmitted over **USART2** of the firmware with the following parameters:

| Parameter        | Value          |
|------------------|----------------|
| Baud rate        | **115200 bps** |
| Word length      | 8 bits         |
| Stop bits        | 1              |
| Parity           | None           |
| Mode             | TX             |
| Flow control     | None           |

---

## State Byte Encoding

The state byte (byte 1) contains both system state and external connection status:

### Bit allocation:
| Bit 7 | Bits 6-0 |
|-------|----------|
| External connection flag | System state |

### Bit definitions:
- **Bit 7 (MSB):** External connection status (does not matter in standalone configuration or older protocol version)
    - `1`: External connection OK (PWM connection established)
    - `0`: External connection NOT OK (PWM connection lost/failed)
- **Bits 6-0 (LSB):** System state value (0-127 range, only 0-9 currently used)

### State values (lower 7 bits):
| Value | Name               |
|-------|--------------------|
| 0     | None               |
| 1     | Testing            |
| 2     | Standby            |
| 3     | Safety Timeout     |
| 4     | Disarmed           |
| 5     | Arming             |
| 6     | Armed              |
| 7     | Fire               |
| 8     | Discharging        |
| 9     | Fired              |

**Example:**
- State byte = `0x86` (binary `10000110`):
    - Bit 7 = `1` → External connection OK
    - Bits 6-0 = `0000110` (6) → System state = "Armed"
- State byte = `0x04` (binary `00000100`):
    - Bit 7 = `0` → External connection NOT OK
    - Bits 6-0 = `0000100` (4) → System state = "Disarmed"

---

## Error Codes

| Value | Name                                |
|-------|-------------------------------------|
| 0     | OK                                  |
| 1     | CRC Error                           |
| 2     | Battery Test Error                  |
| 3     | Safety Sensor Error                 |
| 4     | Safety Switch Error                 |
| 5     | Cap Debounce Error                  |
| 6     | Fuse Error                          |
| 7     | Safety Switch Moved Error           |
| 8     | Boost Error                         |
| 9     | Weak Battery Error                  |
| 10    | IMU Error                           |
| 11    | Prearm Error                        |
| 12    | Discharge Error                     |
| 13    | External Connection Error           |
| 14    | Start Config Error                  |
| 20    | LIDAR Presence Error                |
| 21    | LIDAR False Target Error            |
| 22    | LIDAR No Target Error               |

---

## CRC Parameters

The CRC is calculated by the STM32 hardware CRC unit configured in **8-bit mode**.

- **Width:** 8 bits
- **Polynomial:** `0x07` (x⁸ + x² + x + 1)
- **Initial value:** `0x00`
- **Input data:** 2 bytes (`state`, `error`)
- **Bit order:** MSB first (as transmitted)
- **Final XOR:** `0x00` (none)
- **Check value:** CRC( `01 00` ) = `0x07`
