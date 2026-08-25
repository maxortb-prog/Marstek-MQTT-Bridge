"""
startup.py

Fuehrt die Erstinitialisierungs-Sequenz gegen ein Marstek-Geraet aus:

    1. Marstek.GetDevice   -> device_type ermitteln (falls in der Config leer)
    2. Wifi.GetStatus      -> Netzwerk-Infos
    3. BLE.GetStatus       -> ble_mac ermitteln (falls in der Config leer)
    4. Bat.GetStatus       -> erste Batteriewerte
    5. ES.GetStatus        -> erste Energie-Statuswerte
    6. ES.GetMode          -> aktueller Betriebsmodus
    7. DOD.SET             -> konfigurierten Startwert setzen
    8. Ble.Adv             -> konfigurierten Startzustand setzen ("Ble_block")
    9. Led.Ctrl            -> konfigurierten Startzustand setzen

Jeder Schritt laeuft ueber client.send_init(), nutzt also die
InitRetryPolicy (Basis-Timeout + Inkrement, mehrere Versuche). Zwischen den
einzelnen Kommandos wird eine kurze Pause eingelegt (init.inter_command_delay_s),
um das Geraet waehrend der Initialisierung nicht zu ueberrennen.

Schlaegt irgendein Schritt nach Ausschoepfen aller Init-Versuche fehl, wird
die MarstekCommunicationError-Exception des udp_client durchgereicht - der
Aufrufer (bridge.py) markiert die Bridge dann als "Communication Fail" und
kann ueber den Watchdog neu starten lassen.

Ermittelte Werte (ble_mac, device_type) werden - falls in der Config leer -
sofort persistiert, damit ein Neustart sie nicht erneut ermitteln muss.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from config import MarstekConfig
from udp_client import MarstekUDPClient

logger = logging.getLogger("marstek.startup")


@dataclass
class StartupResult:
    device_type: str
    device_ver: Optional[int]
    wifi_mac: Optional[str]
    wifi_name: Optional[str]
    device_ip: Optional[str]
    ble_mac: str
    ble_state: Optional[str]
    wifi_status: dict
    battery_status: dict
    es_status: dict
    es_mode: dict
    dod_set_result: bool
    ble_adv_set_result: bool
    led_set_result: bool


async def run_startup_sequence(client: MarstekUDPClient, cfg: MarstekConfig) -> StartupResult:
    delay = cfg.get("init", "inter_command_delay_s", default=5.0)

    async def _pause():
        if delay:
            await asyncio.sleep(delay)

    # 1) Marstek.GetDevice --------------------------------------------------
    logger.info("Init: Marstek.GetDevice")
    configured_ble_mac = cfg.get("general", "device_ble_mac", default="") or "0"
    device_info = await client.send_init("Marstek.GetDevice", {"ble_mac": configured_ble_mac})
    device_type = cfg.get("general", "device_type", default="") or device_info.get("device", "")
    if not cfg.get("general", "device_type", default=""):
        cfg.set_and_persist("general", "device_type", value=device_type)
    await _pause()

    # 2) Wifi.GetStatus -------------------------------------------------------
    logger.info("Init: Wifi.GetStatus")
    wifi_status = await client.send_init("Wifi.GetStatus", {"id": 0})
    await _pause()

    # 3) BLE.GetStatus --------------------------------------------------------
    logger.info("Init: BLE.GetStatus")
    ble_status = await client.send_init("BLE.GetStatus", {"id": 0})
    ble_mac = cfg.get("general", "device_ble_mac", default="") or ble_status.get("ble_mac", "")
    if not cfg.get("general", "device_ble_mac", default=""):
        cfg.set_and_persist("general", "device_ble_mac", value=ble_mac)
    await _pause()

    # 4) Bat.GetStatus ----------------------------------------------------
    logger.info("Init: Bat.GetStatus")
    battery_status = await client.send_init("Bat.GetStatus", {"id": 0})
    await _pause()

    # 5) ES.GetStatus -------------------------------------------------------
    logger.info("Init: ES.GetStatus")
    es_status = await client.send_init("ES.GetStatus", {"id": 0})
    await _pause()

    # 6) ES.GetMode ---------------------------------------------------------
    logger.info("Init: ES.GetMode")
    es_mode = await client.send_init("ES.GetMode", {"id": 0})
    await _pause()

    # 7) DOD.SET (Startwert) --------------------------------------------------
    dod_value = int(cfg.get("dod", "startup_value", default=88))
    logger.info("Init: DOD.SET value=%s", dod_value)
    dod_result = await client.send_init("DOD.SET", {"value": dod_value})
    await _pause()

    # 8) Ble.Adv ("Ble_block") Startzustand ------------------------------------
    ble_adv_enable = int(cfg.get("ble_block", "startup_enable", default=0))
    logger.info("Init: Ble.Adv enable=%s", ble_adv_enable)
    ble_adv_result = await client.send_init("Ble.Adv", {"enable": ble_adv_enable})
    await _pause()

    # 9) Led.Ctrl Startzustand -------------------------------------------------
    led_state = int(cfg.get("led", "startup_state", default=0))
    logger.info("Init: Led.Ctrl state=%s", led_state)
    led_result = await client.send_init("Led.Ctrl", {"state": led_state})

    logger.info("Init-Sequenz erfolgreich abgeschlossen (device_type=%s, ble_mac=%s)",
                device_type, ble_mac)

    return StartupResult(
        device_type=device_type,
        device_ver=device_info.get("ver"),
        wifi_mac=device_info.get("wifi_mac"),
        wifi_name=device_info.get("wifi_name"),
        device_ip=device_info.get("ip"),
        ble_mac=ble_mac,
        ble_state=ble_status.get("state"),
        wifi_status=wifi_status,
        battery_status=battery_status,
        es_status=es_status,
        es_mode=es_mode,
        dod_set_result=bool(dod_result.get("set_result")),
        ble_adv_set_result=bool(ble_adv_result.get("set_result")),
        led_set_result=bool(led_result.get("set_result")),
    )
