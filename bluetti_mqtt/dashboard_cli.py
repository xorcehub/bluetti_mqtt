"""
Bluetti Power Station TUI Dashboard.
Live monitoring via Bluetooth BLE using the bluetti_mqtt backend.
"""

import argparse
import asyncio
import json
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
from rich.bar import Bar
from rich.text import Text

from bluetti_mqtt.bluetooth import BluetoothClient, build_device, DEVICE_NAME_RE
from bluetti_mqtt.bluetooth.exc import BadConnectionError, ModbusError, ParseError
from bluetti_mqtt.core.commands import ReadHoldingRegisters

CONFIG_PATH = Path.home() / ".bluetti_dashboard.json"

POLL_INTERVAL = 2.0


# ── Config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(mac: str, name: str) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps({"last_mac": mac, "last_name": name}, indent=2))
    except OSError:
        pass


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
            self.query_one("#scan-status", Label).update("[yellow]No Bluetti devices found. Press R to rescan.[/yellow]")
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
    ]

    def __init__(self, mac: str, name: str):
        super().__init__()
        self.mac = mac
        self.device_name = name
        self._client: Optional[BluetoothClient] = None
        self._device = None
        self._poll_count = 0

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

        # Start BLE client
        self._client = BluetoothClient(self.mac)
        client_task = asyncio.get_running_loop().create_task(self._client.run())

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
                    except (ModbusError, ParseError, BadConnectionError, asyncio.TimeoutError):
                        pass

                if frame:
                    self._poll_count += 1
                    self._update_widgets(frame)

                # Update connection indicator
                if self._client.is_ready:
                    status.update(f"[bold green]Connected[/] — {self.device_name} ({self.mac}) [dim]poll #{self._poll_count} | {len(frame)} fields[/]")
                else:
                    status.update("[yellow]Reconnecting...[/]")

                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(POLL_INTERVAL)

    def _update_widgets(self, data: dict) -> None:
        pct = data.get("total_battery_percent", 0)
        if isinstance(pct, int):
            self.query_one("#battery-widget", BatteryWidget).update(render_battery(pct))

        self.query_one("#info-widget", InfoWidget).update(render_info(data))
        self.query_one("#power-widget", PowerFlowWidget).update(render_power_flow(data))
        self.query_one("#status-widget", StatusWidget).update(render_status(data))

    async def action_rescan(self) -> None:
        self.app.pop_screen()
        self.app.push_screen(ScanScreen())


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
