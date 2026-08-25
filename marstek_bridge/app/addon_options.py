"""
addon_options.py

Bildet die FLACHEN Add-on-Optionen, wie sie der HA-Supervisor in
/data/options.json ablegt (gemaess config.yaml -> 'options'/'schema'),
auf die verschachtelte Struktur ab, die config.MarstekConfig erwartet.

Getrennt von main.py, damit diese Mapping-Logik OHNE Supervisor/Docker
per pytest testbar ist.

Persistenz-Strategie fuer device_ble_mac/device_type:
    Home Assistant behandelt /data/options.json als vom Supervisor verwaltet
    (Aenderungen nur ueber die Add-on-UI). Die Bridge darf diese Datei nicht
    selbst umschreiben. Stattdessen werden bei der Init-Sequenz ermittelte
    Werte in eine EIGENE, kleine State-Datei (/data/marstek_state.yaml)
    geschrieben. Beim naechsten Start werden NUR die Felder
    general.device_ble_mac / general.device_type aus dieser State-Datei
    uebernommen (falls die Optionen selbst leer sind) - alle anderen Werte
    kommen bei jedem Start frisch aus options.json, damit Aenderungen ueber
    die Supervisor-UI sofort wirken und nicht von einem alten State
    ueberschrieben werden.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("marstek.addon_options")


def build_overrides(options: dict, *, mqtt_host_override: Optional[str] = None,
                     mqtt_port_override: Optional[int] = None,
                     mqtt_username_override: Optional[str] = None,
                     mqtt_password_override: Optional[str] = None) -> dict:
    """options: das geparste /data/options.json (flache Struktur).
    Die mqtt_*_override-Parameter kommen typischerweise aus der
    Supervisor-Service-Discovery (bashio::services mqtt ...) und haben
    Vorrang vor manuell in den Optionen eingetragenen Werten, AUSSER der
    Nutzer hat explizit einen eigenen mqtt_host in den Optionen gesetzt."""

    mqtt_host = options.get("mqtt_host") or mqtt_host_override or "core-mosquitto"
    mqtt_port = options.get("mqtt_port") or mqtt_port_override or 1883
    mqtt_username = options.get("mqtt_username") or mqtt_username_override or ""
    mqtt_password = options.get("mqtt_password") or mqtt_password_override or ""

    return {
        "general": {
            "device_ip": options["device_ip"],
            "device_udp_port": int(options["device_udp_port"]),
            "device_ble_mac": options.get("device_ble_mac", "") or "",
            "device_type": options.get("device_type", "") or "",
            "mqtt_host": mqtt_host,
            "mqtt_port": int(mqtt_port),
            "mqtt_username": mqtt_username,
            "mqtt_password": mqtt_password,
            "mqtt_discovery_prefix": options.get("mqtt_discovery_prefix", "homeassistant"),
            "mqtt_base_topic": options.get("mqtt_base_topic", "Marstek-Bridge-Control"),
            "mqtt_suggested_area": options.get("mqtt_suggested_area", "Marstek"),
        },
        "status_polling": {
            "bat_status_interval_s": int(options.get("bat_status_interval_s", 3600)),
            "es_mode_interval_s": int(options.get("es_mode_interval_s", 900)),
            "es_status_interval_s": int(options.get("es_status_interval_s", 300)),
        },
        "passive_mode": {
            "power": int(options.get("passive_power", 50)),
            "cd_time": int(options.get("passive_cd_time", 60)),
            "max_cd_time": int(options.get("passive_max_cd_time", 3600)),
        },
        "controller": {
            "deadzone_w": float(options.get("controller_deadzone_w", 40)),
            "min_setpoint_change_w": float(options.get("controller_min_setpoint_change_w", 50)),
            "max_step_w": float(options.get("controller_max_step_w", 125)),
            "min_output_w": float(options.get("controller_min_output_w", -1500)),
            "max_output_w": float(options.get("controller_max_output_w", 800)),
            "min_send_interval_s": float(options.get("controller_min_send_interval_s", 30)),
        },
        "message_settings": {
            "max_retry": int(options.get("message_max_retry", 3)),
            "timeout_s": float(options.get("message_timeout_s", 1.0)),
        },
        "init": {
            "base_timeout_s": float(options.get("init_base_timeout_s", 2.0)),
            "timeout_increment_s": float(options.get("init_timeout_increment_s", 5.0)),
            "max_retries": int(options.get("init_max_retries", 4)),
            "inter_command_delay_s": float(options.get("init_inter_command_delay_s", 5.0)),
        },
        "dod": {"startup_value": int(options.get("dod_startup_value", 88))},
        "led": {"startup_state": int(options.get("led_startup_state", 0))},
        "ble_block": {"startup_enable": int(options.get("ble_block_startup_enable", 0))},
        "shelly": {"power_topic": options.get("shelly_power_topic", "") or ""},
    }


def apply_persisted_discovery(overrides: dict, state_path: Path) -> dict:
    """Ergaenzt general.device_ble_mac/device_type aus der State-Datei,
    aber NUR falls die Optionen selbst leer sind. Mutiert und gibt
    'overrides' zurueck."""
    if not state_path.exists():
        return overrides
    try:
        state = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("State-Datei %s konnte nicht gelesen werden, ignoriere sie", state_path,
                        exc_info=True)
        return overrides

    persisted_general = state.get("general", {}) if isinstance(state, dict) else {}
    if not overrides["general"].get("device_ble_mac") and persisted_general.get("device_ble_mac"):
        overrides["general"]["device_ble_mac"] = persisted_general["device_ble_mac"]
        logger.info("device_ble_mac aus State-Datei uebernommen: %s", persisted_general["device_ble_mac"])
    if not overrides["general"].get("device_type") and persisted_general.get("device_type"):
        overrides["general"]["device_type"] = persisted_general["device_type"]
        logger.info("device_type aus State-Datei uebernommen: %s", persisted_general["device_type"])
    return overrides
