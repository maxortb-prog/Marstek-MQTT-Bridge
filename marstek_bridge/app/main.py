"""
main.py

Einstiegspunkt fuer das Home-Assistant-Add-on. Wird von run.sh aufgerufen.

Ablauf:
    1. /data/options.json laden (vom Supervisor bereitgestellt)
    2. Optionale MQTT-Service-Discovery-Werte aus Umgebungsvariablen lesen
       (werden von run.sh via bashio::services gesetzt, falls der Nutzer
       das Mosquitto-Add-on mit aktivierter Discovery verwendet)
    3. Optionen -> verschachtelte Config-Struktur (addon_options.py)
    4. Persistierte ble_mac/device_type aus /data/marstek_state.yaml einmischen
    5. MarstekBridge starten und laufen lassen, bis SIGTERM/SIGINT ODER der
       interne Watchdog (nach Ausschoepfen aller Retries) das Ende signalisiert
    6. Bei Watchdog-Abbruch mit Exit-Code != 0 beenden, damit der Supervisor
       (falls "Watchdog" fuer dieses Add-on aktiviert ist) neu startet
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from addon_options import apply_persisted_discovery, build_overrides
from bridge import MarstekBridge
from config import MarstekConfig

OPTIONS_PATH = Path("/data/options.json")
STATE_PATH = Path("/data/marstek_state.yaml")

logger = logging.getLogger("marstek.main")


def _load_options() -> dict:
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


async def _run() -> int:
    options = _load_options()

    log_level = str(options.get("log_level", "info")).upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    overrides = build_overrides(
        options,
        mqtt_host_override=os.environ.get("MARSTEK_MQTT_HOST"),
        mqtt_port_override=int(os.environ["MARSTEK_MQTT_PORT"]) if os.environ.get("MARSTEK_MQTT_PORT") else None,
        mqtt_username_override=os.environ.get("MARSTEK_MQTT_USER"),
        mqtt_password_override=os.environ.get("MARSTEK_MQTT_PASSWORD"),
    )
    apply_persisted_discovery(overrides, STATE_PATH)

    cfg = MarstekConfig.from_dict(overrides, path=STATE_PATH)

    bridge = MarstekBridge(cfg)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await bridge.start()
    try:
        stop_waiter = asyncio.ensure_future(stop_event.wait())
        watchdog_waiter = asyncio.ensure_future(bridge.shutdown_event.wait())
        await asyncio.wait({stop_waiter, watchdog_waiter}, return_when=asyncio.FIRST_COMPLETED)
        for t in (stop_waiter, watchdog_waiter):
            if not t.done():
                t.cancel()
    finally:
        await bridge.stop()

    if bridge.shutdown_event.is_set() and not stop_event.is_set():
        logger.critical("Beende Prozess mit Exit-Code 1 wegen internem Watchdog "
                         "(Add-on-Watchdog in HA aktivieren, damit automatisch neu gestartet wird)")
        return 1
    return 0


def main() -> None:
    exit_code = asyncio.run(_run())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
