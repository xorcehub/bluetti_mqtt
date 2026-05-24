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
from textual.reactive import reactive
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
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, auto_mac: Optional[str] = None, auto_name: Optional[str] = None):
        super().__init__()
        self.auto_mac = auto_mac
        self.auto_name = auto_name
        self._scanned: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
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
    """Battery percentage with visual bar."""

    percent: reactive[int] = reactive(0)

    def watch_percent(self, val: int) -> None:
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
        self.update(f"[bold]{val:3d}%[/bold] {bar_str}")


class PowerFlowWidget(Static):
    """Shows input/output power with arrows."""

    data: reactive[dict] = reactive({})

    def watch_data(self, val: dict) -> None:
        dc_in = val.get("dc_input_power", 0)
        ac_in = val.get("ac_input_power", 0)
        ac_out = val.get("ac_output_power", 0)
        dc_out = val.get("dc_output_power", 0)
        total_in = dc_in + ac_in
        net = total_in - ac_out - dc_out

        lines = []
        if dc_in > 0:
            lines.append(f"  DC In (solar): [bold green]+{dc_in}W[/]")
        else:
            lines.append(f"  DC In (solar):  [dim]0W[/]")
        if ac_in > 0:
            lines.append(f"  AC In:         [bold green]+{ac_in}W[/]")
        else:
            lines.append(f"  AC In:          [dim]0W[/]")
        if ac_out > 0:
            lines.append(f"  AC Out:        [bold red]-{ac_out}W[/]")
        else:
            lines.append(f"  AC Out:         [dim]0W[/]")
        if dc_out > 0:
            lines.append(f"  DC Out:        [bold red]-{dc_out}W[/]")
        else:
            lines.append(f"  DC Out:         [dim]0W[/]")

        lines.append("")
        if net >= 0:
            lines.append(f"  Net: [bold green]+{net}W[/] (charging)")
        else:
            lines.append(f"  Net: [bold red]{net}W[/] (discharging)")

        self.update("\n".join(lines))


class StatusWidget(Static):
    """On/off status flags."""

    data: reactive[dict] = reactive({})

    def watch_data(self, val: dict) -> None:
        def _flag(name: str, key: str) -> str:
            v = val.get(key)
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

        eco = val.get("eco_shutdown")
        if eco is not None:
            lines.append(f"  Eco Timeout: {eco.name.replace('_', ' ').title()}")

        charge = val.get("charging_mode")
        if charge is not None:
            lines.append(f"  Charging: {charge.name.title()}")

        led = val.get("led_mode")
        if led is not None:
            lines.append(f"  LED: {led.name.title()}")

        self.update("\n".join(lines))


class InfoWidget(Static):
    """Device info header."""

    data: reactive[dict] = reactive({})

    def watch_data(self, val: dict) -> None:
        dtype = val.get("device_type", "???")
        sn = val.get("serial_number", "---")
        arm = val.get("arm_version", "---")
        dsp = val.get("dsp_version", "---")
        ac_v = val.get("ac_input_voltage", "---")
        dc_v = val.get("internal_dc_input_voltage", "---")
        self.update(
            f"[bold]{dtype}[/]  SN: {sn}\n"
            f"ARM: {arm}  DSP: {dsp}\n"
            f"AC In Voltage: {ac_v}V  DC In Voltage: {dc_v}V"
        )


# ── Dashboard Screen ────────────────────────────────────────────────────────

class DashboardScreen(Screen):
    """Live data dashboard with BLE polling."""

    BINDINGS = [
        Binding("r", "rescan", "Rescan"),
        Binding("q", "quit", "Quit"),
        Binding("d", "disconnect", "Disconnect"),
    ]

    def __init__(self, mac: str, name: str):
        super().__init__()
        self.mac = mac
        self.device_name = name
        self._client: Optional[BluetoothClient] = None
        self._device = None
        self._poll_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
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

    async def on_mount(self) -> None:
        self._poll_task = asyncio.create_task(self._connect_and_poll())

    def on_unmount(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

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
        merged: dict = {}
        while True:
            try:
                for cmd in self._device.polling_commands:
                    try:
                        future = await self._client.perform(cmd)
                        response = await asyncio.wait_for(future, timeout=10.0)
                        body = cmd.parse_response(response)
                        parsed = self._device.parse(cmd.starting_address, body)
                        merged.update(parsed)
                    except (ModbusError, ParseError, BadConnectionError, asyncio.TimeoutError):
                        pass

                self._update_widgets(merged)

                # Update connection indicator
                if self._client.is_ready:
                    status.update(f"[bold green]Connected[/] — {self.device_name} ({self.mac})")
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
            self.query_one("#battery-widget", BatteryWidget).percent = pct

        self.query_one("#info-widget", InfoWidget).data = data
        self.query_one("#power-widget", PowerFlowWidget).data = data
        self.query_one("#status-widget", StatusWidget).data = data

    async def action_rescan(self) -> None:
        self.app.pop_screen()
        self.app.push_screen(ScanScreen(auto_mac=None, auto_name=None))

    async def action_disconnect(self) -> None:
        self.app.pop_screen()
        self.app.push_screen(ScanScreen(auto_mac=None, auto_name=None))


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
