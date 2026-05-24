"""
Bluetti Power Station TUI Dashboard.
Live monitoring via Bluetooth BLE using the bluetti_mqtt backend.
"""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from bleak import BleakScanner
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from bluetti_mqtt.bluetooth import BluetoothClient, build_device, DEVICE_NAME_RE
from bluetti_mqtt.bluetooth.exc import BadConnectionError, ModbusError, ParseError
from bluetti_mqtt.core.devices.eb3a import ChargingMode, LedMode

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "dashboard.json"
LOG_DIR = PROJECT_DIR / "logs"

POLL_INTERVAL = 2.0
SPARKLINE_WIDTH = 50

DEVICE_CAPACITY_WH = {
    "EB3A": 268,
    "AC200M": 2048,
    "AC300": 3000,
    "AC500": 4600,
    "AC60": 403,
    "EP500": 5100,
    "EP500P": 5100,
    "EP600": 5120,
}

DEFAULT_LOG_INTERVAL = 30
DEFAULT_HISTORY_HOURS = 6
DEFAULT_SPARKLINE_WIDTH = 50
DEFAULT_ALERT_FULL = 100
DEFAULT_ALERT_LOW = 20
DEFAULT_ALERT_CRITICAL = 10

SPARK_CHARS = "▁▂▃▄▅▆▇█"


# ── Config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            cfg.setdefault("log_interval", DEFAULT_LOG_INTERVAL)
            cfg.setdefault("logging_enabled", True)
            cfg.setdefault("history_hours", DEFAULT_HISTORY_HOURS)
            cfg.setdefault("sparkline_width", DEFAULT_SPARKLINE_WIDTH)
            cfg.setdefault("alert_full", DEFAULT_ALERT_FULL)
            cfg.setdefault("alert_low", DEFAULT_ALERT_LOW)
            cfg.setdefault("alert_critical", DEFAULT_ALERT_CRITICAL)
            cfg.setdefault("led_sos_full", False)
            cfg.setdefault("led_sos_low", False)
            cfg.setdefault("led_sos_critical", False)
            return cfg
        except (json.JSONDecodeError, OSError):
            pass
    return {"log_interval": DEFAULT_LOG_INTERVAL, "logging_enabled": True}


def save_config(mac: str = None, name: str = None, **kwargs) -> None:
    cfg = load_config()
    if mac is not None:
        cfg["last_mac"] = mac
    if name is not None:
        cfg["last_name"] = name
    cfg.update(kwargs)
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


# ── Logging ─────────────────────────────────────────────────────────────────

def log_path(device_type: str) -> Path:
    return LOG_DIR / device_type / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def append_log(device_type: str, data: dict) -> None:
    path = log_path(device_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(),
        "battery": data.get("total_battery_percent"),
        "dc_in": data.get("dc_input_power", 0),
        "ac_in": data.get("ac_input_power", 0),
        "ac_out": data.get("ac_output_power", 0),
        "dc_out": data.get("dc_output_power", 0),
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except (OSError, TypeError):
        pass


def load_today_log(device_type: str) -> list[dict]:
    path = log_path(device_type)
    if not path.exists():
        return []
    entries = []
    try:
        for line in path.read_text().splitlines():
            if line.strip():
                entries.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        pass
    # Only keep last 6 hours
    cutoff = datetime.now().timestamp() - load_config().get("history_hours", DEFAULT_HISTORY_HOURS) * 3600
    return [e for e in entries if datetime.fromisoformat(e["ts"]).timestamp() >= cutoff]


# ── Sparkline ───────────────────────────────────────────────────────────────

def render_sparkline(values: list[float], width: int = None, label: str = "") -> str:
    if width is None:
        width = load_config().get("sparkline_width", DEFAULT_SPARKLINE_WIDTH)
    if not values:
        return f"  {label}[dim]no data yet[/]"

    lo = min(values)
    hi = max(values)

    n = len(values)
    if n > width:
        step = n / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values

    chars = []
    for v in sampled:
        idx = min(int(v / 100 * (len(SPARK_CHARS) - 1)), len(SPARK_CHARS) - 1)
        idx = max(idx, 0)
        chars.append(SPARK_CHARS[idx])

    return f"  {label}[green]{''.join(chars)}[/] [dim]{lo:.0f}–{hi:.0f}%[/]"


# ── Time estimate ───────────────────────────────────────────────────────────

def render_time_estimate(data: dict) -> str:
    pct = data.get("total_battery_percent", 0)
    dtype = data.get("device_type", "")
    if not isinstance(pct, int) or pct < 0:
        return ""

    capacity_wh = DEVICE_CAPACITY_WH.get(dtype)
    if not capacity_wh:
        return ""

    dc_in = (data.get("dc_input_power") or 0)
    ac_in = (data.get("ac_input_power") or 0)
    ac_out = (data.get("ac_output_power") or 0)
    dc_out = (data.get("dc_output_power") or 0)
    net = dc_in + ac_in - ac_out - dc_out

    if net == 0:
        return "  Estimate: [dim]no power flow[/]"

    if net > 0:
        remaining_wh = capacity_wh * (100 - pct) / 100
        hours = remaining_wh / net
        return f"  Estimate: [bold green]{_fmt_hours(hours)}[/] until full ({net:+d}W)"
    else:
        remaining_wh = capacity_wh * pct / 100
        hours = remaining_wh / abs(net)
        return f"  Estimate: [bold red]{_fmt_hours(hours)}[/] until empty ({net:+d}W)"


def _fmt_hours(h: float) -> str:
    if h < 1:
        return f"{int(h * 60)}min"
    hrs = int(h)
    mins = int((h - hrs) * 60)
    if mins:
        return f"{hrs}h {mins}min"
    return f"{hrs}h"


# ── Scan Screen ─────────────────────────────────────────────────────────────

class ScanScreen(Screen):
    """BLE scan screen — discover and select a Bluetti device."""

    BINDINGS = [
        Binding("r", "rescan", "Rescan"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, auto_mac: Optional[str] = None, auto_name: Optional[str] = None):
        super().__init__()
        self.auto_mac = auto_mac
        self.auto_name = auto_name
        self._scanned: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="⚡")
        with Vertical(id="scan-container"):
            yield Label("[bold]Bluetti Dashboard[/bold]", id="scan-title")
            yield Label("Scanning for Bluetti devices...", id="scan-status")
            yield ListView(id="device-list")
            with Horizontal(id="manual-row"):
                yield Input(placeholder="Or enter MAC manually (XX:XX:XX:XX:XX:XX)", id="mac-input")
                yield Button("Connect", variant="primary", id="connect-btn")
        yield Footer()

    async def on_mount(self) -> None:
        if self.auto_mac and self.auto_name:
            self.app.push_screen(DashboardScreen(self.auto_mac, self.auto_name))
            return
        await self.action_rescan()

    async def action_rescan(self) -> None:
        self.query_one("#scan-status", Label).update("Scanning...")
        self.query_one("#device-list", ListView).clear()
        self._scanned = []

        try:
            devices = await BleakScanner.discover(timeout=8.0)
        except Exception as e:
            self.query_one("#scan-status", Label).update(f"[red]Scan failed: {e}[/red]")
            return

        bluetti = [
            (d.address, d.name)
            for d in devices
            if d.name and DEVICE_NAME_RE.match(d.name)
        ]

        lv = self.query_one("#device-list", ListView)
        if not bluetti:
            self.query_one("#scan-status", Label).update(
                "[yellow]No Bluetti devices found. Press R to rescan.[/yellow]"
            )
            return

        self._scanned = bluetti
        for addr, name in bluetti:
            lv.append(ListItem(Label(f"{name}  [dim]{addr}[/dim]")))

        self.query_one("#scan-status", Label).update(
            f"Found {len(bluetti)} device(s). Select one or press R to rescan."
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = self.query_one("#device-list", ListView).children.index(event.item)
        if idx < len(self._scanned):
            addr, name = self._scanned[idx]
            save_config(addr, name)
            self.app.push_screen(DashboardScreen(addr, name))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect-btn":
            mac = self.query_one("#mac-input", Input).value.strip().upper()
            if len(mac) == 17 and mac.count(":") == 5:
                save_config(mac, "Unknown")
                self.app.push_screen(DashboardScreen(mac, "Manual"))
            else:
                self.query_one("#scan-status", Label).update("[red]Invalid MAC format[/red]")


# ── Dashboard Widgets ───────────────────────────────────────────────────────

class BatteryWidget(Static):
    pass


class PowerFlowWidget(Static):
    pass


class StatusWidget(Static):
    pass


class InfoWidget(Static):
    pass


class SparklineWidget(Static):
    pass


class AlertWidget(Static):
    pass


def render_alert(pct: int, cfg: dict) -> str:
    if pct <= cfg.get("alert_critical", DEFAULT_ALERT_CRITICAL):
        return f"[bold red]!! Battery critical ({pct}%) !![/]"
    if pct <= cfg.get("alert_low", DEFAULT_ALERT_LOW):
        return f"[bold yellow]! Battery low ({pct}%)[/]"
    if pct >= cfg.get("alert_full", DEFAULT_ALERT_FULL):
        return f"[bold green]Fully charged ({pct}%)[/]"
    return ""


def render_battery(val: int) -> str:
    bar_len = 30
    filled = int(bar_len * val / 100)
    empty = bar_len - filled
    if val >= 80:
        color = "green"
    elif val >= 40:
        color = "yellow"
    else:
        color = "red"
    bar_str = f"[{color}]{'█' * filled}[/][dim]{'░' * empty}[/]"
    return f"[bold]{val:3d}%[/bold] {bar_str}"


def render_power_flow(d: dict) -> str:
    dc_in = d.get("dc_input_power", 0) or 0
    ac_in = d.get("ac_input_power", 0) or 0
    ac_out = d.get("ac_output_power", 0) or 0
    dc_out = d.get("dc_output_power", 0) or 0
    net = dc_in + ac_in - ac_out - dc_out

    lines = [
        f"  DC In (solar): {'[bold green]+' + str(dc_in) + 'W[/]' if dc_in else '[dim]0W[/]'}",
        f"  AC In:         {'[bold green]+' + str(ac_in) + 'W[/]' if ac_in else '[dim]0W[/]'}",
        f"  AC Out:        {'[bold red]-' + str(ac_out) + 'W[/]' if ac_out else '[dim]0W[/]'}",
        f"  DC Out:        {'[bold red]-' + str(dc_out) + 'W[/]' if dc_out else '[dim]0W[/]'}",
        "",
        f"  Net: [bold green]+{net}W[/] (charging)" if net >= 0 else f"  Net: [bold red]{net}W[/] (discharging)",
        render_time_estimate(d),
    ]
    return "\n".join(lines)


def render_status(d: dict) -> str:
    def _flag(name, key):
        v = d.get(key)
        if v is True:
            return f"  {name}: [bold green]ON[/]"
        elif v is False:
            return f"  {name}: [dim]OFF[/]"
        return f"  {name}: [dim]--[/]"

    lines = [
        _flag("AC Output", "ac_output_on"),
        _flag("DC Output", "dc_output_on"),
        _flag("Eco Mode", "eco_on"),
        _flag("Power Lifting", "power_lifting_on"),
    ]
    eco = d.get("eco_shutdown")
    if eco is not None:
        lines.append(f"  Eco Timeout: {eco.name.replace('_', ' ').title()}")
    charge = d.get("charging_mode")
    if charge is not None:
        lines.append(f"  Charging: {charge.name.title()}")
    led = d.get("led_mode")
    if led is not None:
        lines.append(f"  LED: {led.name.title()}")
    return "\n".join(lines)


def render_info(d: dict) -> str:
    dtype = d.get("device_type", "???")
    sn = d.get("serial_number", "---")
    arm = d.get("arm_version", "---")
    dsp = d.get("dsp_version", "---")
    ac_v = d.get("ac_input_voltage", "---")
    dc_v = d.get("internal_dc_input_voltage", "---")
    return (
        f"[bold]{dtype}[/]  SN: {sn}\n"
        f"ARM: {arm}  DSP: {dsp}\n"
        f"AC In Voltage: {ac_v}V  DC In Voltage: {dc_v}V"
    )


# ── Dashboard Screen ────────────────────────────────────────────────────────

class DashboardScreen(Screen):
    """Live data dashboard with BLE polling."""

    BINDINGS = [
        Binding("r", "rescan", "Rescan"),
        Binding("q", "app.quit", "Quit"),
        Binding("l", "toggle_logging", "Toggle log"),
        Binding("a", "toggle_ac", "AC on/off"),
        Binding("d", "toggle_dc", "DC on/off"),
        Binding("e", "toggle_eco", "Eco on/off"),
        Binding("c", "cycle_charging", "Charge mode"),
        Binding("m", "cycle_led", "LED mode"),
    ]

    def __init__(self, mac: str, name: str):
        super().__init__()
        self.mac = mac
        self.device_name = name
        self._client: Optional[BluetoothClient] = None
        self._device = None
        self._poll_count = 0
        self._battery_history: list[float] = []
        self._last_log_time: float = 0
        self._last_sparkline_time: float = 0
        self._last_frame: dict = {}
        self._cfg = load_config()
        self._led_alert_active: bool = False
        self._led_mode_before_alert = None
        self._prev_pct: Optional[int] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="⚡")
        with Vertical(id="dash-container"):
            yield Label(f"Connecting to {self.device_name}...", id="connection-status")
            with Horizontal(id="dash-main"):
                with Vertical(id="dash-left"):
                    yield Label("[bold]Device[/bold]", classes="section-title")
                    yield InfoWidget(id="info-widget")
                    yield Label("")
                    yield Label("[bold]Battery[/bold]", classes="section-title")
                    yield BatteryWidget(id="battery-widget")
                with Vertical(id="dash-right"):
                    yield Label("[bold]Power Flow[/bold]", classes="section-title")
                    yield PowerFlowWidget(id="power-widget")
                    yield Label("")
                    yield Label("[bold]Status[/bold]", classes="section-title")
                    yield StatusWidget(id="status-widget")
            yield Label("")
            history_h = load_config().get('history_hours', DEFAULT_HISTORY_HOURS)
            yield Label(
                f"[bold]Battery History (last {history_h}h)[/bold]",
                classes="section-title",
            )
            yield SparklineWidget(id="sparkline-widget")
            yield AlertWidget(id="alert-widget")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._connect_and_poll(), exclusive=True)

    def on_unmount(self) -> None:
        if self._client and self._client.client:
            asyncio.get_event_loop().create_task(self._client.client.disconnect())

    async def _connect_and_poll(self) -> None:
        status = self.query_one("#connection-status", Label)

        # Discover and build device
        try:
            devices = await BleakScanner.discover(timeout=8.0)
            matched = [d for d in devices if d.address == self.mac]
            if not matched:
                status.update(f"[red]Device {self.mac} not found. Press R to rescan.[/red]")
                return

            ble_dev = matched[0]
            if ble_dev.name and DEVICE_NAME_RE.match(ble_dev.name):
                self._device = build_device(self.mac, ble_dev.name)
                self.device_name = ble_dev.name
            else:
                status.update(f"[red]Device {self.mac} is not a recognized Bluetti.[/red]")
                return
        except Exception as e:
            status.update(f"[red]Scan error: {e}[/red]")
            return

        # Load today's log history for sparkline
        dtype = self._device.type if self._device else ""
        history = load_today_log(dtype)
        self._battery_history = [e["battery"] for e in history if e.get("battery") is not None]
        if self._battery_history:
            self.query_one("#sparkline-widget", SparklineWidget).update(
                render_sparkline(self._battery_history)
            )

        # Start BLE client
        self._client = BluetoothClient(self.mac)
        asyncio.get_running_loop().create_task(self._client.run())

        # Wait for ready
        status.update(f"Connecting to {self.device_name}...")
        for _ in range(30):
            if self._client.is_ready:
                break
            await asyncio.sleep(1)
        else:
            status.update("[red]Connection timed out. Press R to rescan.[/red]")
            return

        status.update(f"[bold green]Connected[/] — {self.device_name} ({self.mac})")
        save_config(self.mac, self.device_name)

        # Poll loop
        while True:
            try:
                frame = {}
                for cmd in self._device.polling_commands:
                    try:
                        future = await self._client.perform(cmd)
                        response = await asyncio.wait_for(future, timeout=10.0)
                        body = cmd.parse_response(response)
                        parsed = self._device.parse(cmd.starting_address, body)
                        frame.update(parsed)
                    except (
                        ModbusError, ParseError, BadConnectionError,
                        asyncio.TimeoutError,
                    ):
                        pass

                if frame:
                    self._poll_count += 1
                    self._update_widgets(frame)

                    # Log at configured interval
                    now = asyncio.get_event_loop().time()
                    log_interval = self._cfg.get("log_interval", DEFAULT_LOG_INTERVAL)
                    if (self._cfg.get("logging_enabled", True)
                            and (now - self._last_log_time) >= log_interval):
                        append_log(dtype, frame)
                        self._last_log_time = now

                    # Update sparkline at same interval
                    pct = frame.get("total_battery_percent")
                    if (isinstance(pct, (int, float))
                            and (now - self._last_sparkline_time) >= log_interval):
                        self._battery_history.append(float(pct))
                        history_h = self._cfg.get(
                            "history_hours", DEFAULT_HISTORY_HOURS
                        )
                        max_points = int(
                            history_h * 3600 / log_interval
                        )
                        if len(self._battery_history) > max_points:
                            self._battery_history = self._battery_history[-max_points:]
                        self.query_one("#sparkline-widget", SparklineWidget).update(
                            render_sparkline(self._battery_history)
                        )
                        self._last_sparkline_time = now

                # Update connection indicator
                log_state = (
                    "log:on" if self._cfg.get("logging_enabled", True)
                    else "log:off"
                )
                if self._client.is_ready:
                    status.update(
                        f"[bold green]Connected[/] — {self.device_name}"
                        f" ({self.mac}) [dim]poll #{self._poll_count}"
                        f" | {log_state}[/]"
                    )
                else:
                    status.update("[yellow]Reconnecting...[/]")

                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(POLL_INTERVAL)

    def _update_widgets(self, data: dict) -> None:
        self._last_frame = data
        pct = data.get("total_battery_percent", 0)
        if isinstance(pct, int):
            self.query_one("#battery-widget", BatteryWidget).update(render_battery(pct))
            alert = render_alert(pct, self._cfg)
            self.query_one("#alert-widget", AlertWidget).update(alert)
            self._check_led_alert(pct, data)

        self.query_one("#info-widget", InfoWidget).update(render_info(data))
        self.query_one("#power-widget", PowerFlowWidget).update(render_power_flow(data))
        self.query_one("#status-widget", StatusWidget).update(render_status(data))

    def _in_alert_range(self, pct: int) -> bool:
        cfg = self._cfg
        if pct >= cfg.get("alert_full", DEFAULT_ALERT_FULL) and cfg.get("led_sos_full", False):
            return True
        if pct <= cfg.get("alert_low", DEFAULT_ALERT_LOW) and cfg.get("led_sos_low", False):
            return True
        if pct <= cfg.get("alert_critical", DEFAULT_ALERT_CRITICAL) and cfg.get("led_sos_critical", False):
            return True
        return False

    def _check_led_alert(self, pct: int, data: dict) -> None:
        if not self._device or not self._client or not self._client.is_ready:
            return

        # Skip on first poll — only trigger on actual threshold crossings
        if self._prev_pct is None:
            self._prev_pct = pct
            return

        was_in = self._in_alert_range(self._prev_pct)
        now_in = self._in_alert_range(pct)
        self._prev_pct = pct

        if now_in and not was_in and not self._led_alert_active:
            # Crossed into alert range
            self._led_mode_before_alert = data.get("led_mode")
            asyncio.get_event_loop().create_task(
                self._send_command("led_mode", "SOS")
            )
            self._led_alert_active = True
        elif not now_in and was_in and self._led_alert_active:
            # Crossed out of alert range
            restore = self._led_mode_before_alert or LedMode.OFF
            mode_name = restore.name if isinstance(restore, LedMode) else "OFF"
            asyncio.get_event_loop().create_task(
                self._send_command("led_mode", mode_name)
            )
            self._led_alert_active = False

    async def _send_command(self, field: str, value) -> None:
        if not self._device or not self._client or not self._client.is_ready:
            return
        try:
            cmd = self._device.build_setter_command(field, value)
            await self._client.perform_nowait(cmd)
        except Exception:
            pass

    async def action_toggle_ac(self) -> None:
        current = self._last_frame.get("ac_output_on")
        if current is not None:
            await self._send_command("ac_output_on", not current)

    async def action_toggle_dc(self) -> None:
        current = self._last_frame.get("dc_output_on")
        if current is not None:
            await self._send_command("dc_output_on", not current)

    async def action_toggle_eco(self) -> None:
        current = self._last_frame.get("eco_on")
        if current is not None:
            await self._send_command("eco_on", not current)

    async def action_cycle_charging(self) -> None:
        current = self._last_frame.get("charging_mode")
        if current is not None:
            modes = list(ChargingMode)
            idx = modes.index(current) if current in modes else 0
            next_mode = modes[(idx + 1) % len(modes)]
            await self._send_command("charging_mode", next_mode.name)

    async def action_cycle_led(self) -> None:
        current = self._last_frame.get("led_mode")
        if current is not None:
            modes = list(LedMode)
            idx = modes.index(current) if current in modes else 0
            next_mode = modes[(idx + 1) % len(modes)]
            await self._send_command("led_mode", next_mode.name)

    async def action_rescan(self) -> None:
        self.app.pop_screen()
        self.app.push_screen(ScanScreen())

    def action_toggle_logging(self) -> None:
        current = self._cfg.get("logging_enabled", True)
        self._cfg["logging_enabled"] = not current
        save_config(logging_enabled=not current)


# ── Main App ────────────────────────────────────────────────────────────────

class BluettiDashboard(App):
    TITLE = "Bluetti Dashboard"
    CSS_PATH = Path(__file__).parent / "dashboard.tcss"

    def on_mount(self) -> None:
        cfg = load_config()
        if cfg.get("last_mac") and cfg.get("last_name"):
            self.push_screen(ScanScreen(auto_mac=cfg["last_mac"], auto_name=cfg["last_name"]))
        else:
            self.push_screen(ScanScreen())


# ── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bluetti Power Station TUI Dashboard")
    parser.parse_args()

    app = BluettiDashboard()
    app.run()


if __name__ == "__main__":
    main()
