# Bluetti Dashboard

A terminal-based TUI dashboard for live monitoring and control of Bluetti power stations via Bluetooth BLE. Built on top of the [bluetti_mqtt](https://github.com/warhammerkid/bluetti_mqtt) library.

## Features

- **Device scanning** — automatic BLE discovery of Bluetti devices, or enter MAC manually
- **Auto-reconnect** — remembers last device, reconnects on startup
- **Live monitoring** — battery, power flow, device info, status (2s poll interval)
- **Persistent logging** — JSONL logs with configurable interval (default 30s)
- **Battery history** — sparkline chart showing last 6 hours (configurable)
- **Time estimates** — charge/discharge time based on current power flow and device capacity
- **Control commands** — toggle AC/DC output, eco mode, charging mode, LED mode
- **Battery alerts** — visual alerts for full/low/critical thresholds
- **LED SOS alerts** — automatically activate LED SOS when battery hits a threshold
- **Bluetooth resilience** — automatic reconnection after BT adapter toggles or transient errors

## Supported Devices

**Monitoring** works with all devices supported by the bluetti_mqtt library (EB3A, AC200M, AC300, AC500, AC60, EP500, EP600, etc.).

**Control commands** currently only support the EB3A. Other devices use different register mappings and enum values for charging mode, LED mode, etc.

## Requirements

- Python 3.10+
- Bluetooth adapter (tested on Windows 11)
- A Bluetti device within BLE range

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS
pip install -e .
```

## Usage

```bash
bluetti-dashboard
```

On first run, the dashboard scans for nearby Bluetti devices. Select one from the list or enter a MAC address manually. The device is saved to `dashboard.json` and auto-connects on subsequent runs.

### Key Bindings

| Key | Action |
|-----|--------|
| `R` | Rescan / go back to scan screen |
| `L` | Toggle data logging on/off |
| `A` | Toggle AC output on/off |
| `D` | Toggle DC output on/off |
| `E` | Toggle eco mode on/off |
| `C` | Cycle charging mode (Standard → Silent → Turbo) |
| `M` | Cycle LED mode (Low → High → SOS → Off) |
| `Q` | Quit |

### Status Bar

The connection status shows: connection state, device name, MAC, poll counter, and logging state.

- `poll #5 | log:on` — connected, logging active
- `poll #5 | log:off` — connected, logging disabled
- `No data (3 stale)` — connected but no data flowing, will restart connection if persistent
- `Reconnecting...` — BLE link down, attempting to reconnect

## Configuration

All settings are stored in `dashboard.json` in the project root. Edit directly or let the dashboard manage it.

```json
{
  "last_mac": "AA:BB:CC:DD:EE:FF",
  "last_name": "EB3A230301XXXXXX",
  "log_interval": 30,
  "logging_enabled": true,
  "history_hours": 6,
  "sparkline_width": 50,
  "alert_full": 100,
  "alert_low": 20,
  "alert_critical": 10,
  "led_sos_full": false,
  "led_sos_low": false,
  "led_sos_critical": false
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `last_mac` | — | Last connected device MAC (auto-saved) |
| `last_name` | — | Last connected device name (auto-saved) |
| `log_interval` | 30 | Seconds between log entries |
| `logging_enabled` | true | Whether logging is active |
| `history_hours` | 6 | Hours of history shown in sparkline |
| `sparkline_width` | 50 | Character width of sparkline |
| `alert_full` | 100 | Battery % for "fully charged" alert |
| `alert_low` | 20 | Battery % for "battery low" alert |
| `alert_critical` | 10 | Battery % for "battery critical" alert |
| `led_sos_full` | false | Activate LED SOS when battery reaches full threshold |
| `led_sos_low` | false | Activate LED SOS when battery reaches low threshold |
| `led_sos_critical` | false | Activate LED SOS when battery reaches critical threshold |

## Data Logging

Logs are stored in `logs/{DEVICE_TYPE}/YYYY-MM-DD.jsonl`. Each entry:

```json
{"ts": "2026-05-25T14:30:00.123456", "battery": 85, "dc_in": 45, "ac_in": 0, "ac_out": 98, "dc_out": 12}
```

| Field | Description |
|-------|-------------|
| `ts` | ISO timestamp |
| `battery` | Battery percentage |
| `dc_in` | DC input power (W) — typically solar |
| `ac_in` | AC input power (W) |
| `ac_out` | AC output power (W) |
| `dc_out` | DC output power (W) |

## How It Works

### Architecture

```
bluetti-dashboard (TUI)
  └── BluetoothClient (state machine)
        └── BleakClient (BLE)
              └── MODBUS-over-Bluetooth
                    └── Bluetti device registers
```

- **Textual** TUI framework with workers for async BLE polling
- **Bleak** for cross-platform BLE communication
- **MODBUS function code 3** (Read Holding Registers) for data, function code 6 (Write Single Register) for control
- Device register mappings defined per model in `bluetti_mqtt/core/devices/`

### Data Flow

1. BLE scan discovers device, identifies model from BLE name
2. `BluetoothClient.run()` state machine connects, subscribes to GATT notifications
3. Dashboard poll loop sends `ReadHoldingRegisters` commands every 2 seconds
4. Responses are parsed by device-specific struct definitions into named fields
5. Widgets update directly via Textual's `update()` method

### Power Values

Power values (watts) are read directly from MODBUS registers as unsigned 16-bit integers with no scaling. The device reports them as-is. If values differ from the device display, it's likely a sampling timing difference — the dashboard reads instantaneously every 2 seconds while the device display may average over a longer window.

### Bluetooth Reconnection

The dashboard handles BT adapter toggles (disable/enable Bluetooth in Windows) by:
1. Detecting stale data (no successful polls for ~30 seconds)
2. Tearing down the client completely (cancel run task, disconnect BLE)
3. Rescanning for the device with retries every 10 seconds
4. Starting a fresh connection from scratch

This bypasses issues with the Windows BLE stack where GATT notification subscriptions don't properly re-establish after an adapter cycle.

## Limitations

- **Control commands are EB3A-only** — other devices need device-specific enum imports
- **No temperature/cell voltage data** — EB3A has 74 undefined registers (136-209) that would need reverse engineering
- **Power values are instantaneous** — no averaging or smoothing
- **Single device** — can only monitor one device at a time
- **No remote access** — requires BLE range from the computer

## Future Improvements

- [ ] Device-agnostic control commands (look up enums from device class at runtime)
- [ ] Reverse engineer EB3A registers 136-209 for temperature, cell voltages, battery current
- [ ] More data fields in logs once discovered
- [ ] Solar shading detection — alert when DC input fluctuates beyond a configurable % within a configurable time window, indicating partial shading (with optional LED SOS)
- [ ] Multiple device support
- [ ] Power lifting toggle (P key) — EB3A supports it, just needs a binding
- [ ] Export logs to CSV

## Project Structure

```
bluetti_mqtt/
├── dashboard_cli.py          # TUI dashboard application
├── dashboard.tcss            # Textual CSS styles
├── bluetooth/
│   └── client.py             # BLE client with MODBUS state machine
├── core/
│   ├── commands.py           # MODBUS read/write command framing
│   ├── devices/
│   │   ├── bluetti_device.py # Base device class
│   │   ├── eb3a.py           # EB3A register definitions
│   │   └── ...               # Other device definitions
│   └── struct.py             # Register-to-field parsing (UintField, DecimalField, etc.)
```

## License

Same as the upstream bluetti_mqtt project.
