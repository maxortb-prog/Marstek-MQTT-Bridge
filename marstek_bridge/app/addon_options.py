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
    Nutzer hat explizit einen eigenen mqtt_host in den Optionen gesetzt.

    Seit HA-Supervisor-Unterstuetzung fuer verschachtelte Add-on-Optionen
    (HA >= 2025.10) liegen die Werte in benannten Gruppen (siehe
    config.yaml): marstek_device, scanrate_statuscalls, passiv_mode_settings,
    selfconsumption_control, message_settings, init_settings, dod_settings,
    additional_settings, mqtt_settings. mqtt_port liegt bewusst NICHT in
    der mqtt_settings-Gruppe (siehe config.yaml-Kommentar: ein Default
    wuerde die Service-Discovery-Fallback-Logik unten verhindern). log_level
    bleibt ebenfalls top-level (wird direkt in main.py gelesen, nicht ueber
    diese Funktion)."""

    device = options.get("marstek_device", {}) or {}
    mqtt_group = options.get("mqtt_settings", {}) or {}
    mqtt_host = mqtt_group.get("mqtt_host") or mqtt_host_override or "core-mosquitto"
    mqtt_port = options.get("mqtt_port") or mqtt_port_override or 1883
    mqtt_username = mqtt_group.get("mqtt_username") or mqtt_username_override or ""
    mqtt_password = mqtt_group.get("mqtt_password") or mqtt_password_override or ""

    scan = options.get("scanrate_statuscalls", {}) or {}
    passive = options.get("passiv_mode_settings", {}) or {}
    ctrl = options.get("selfconsumption_control", {}) or {}
    msg = options.get("message_settings", {}) or {}
    init = options.get("init_settings", {}) or {}
    dod = options.get("dod_settings", {}) or {}
    extra = options.get("additional_settings", {}) or {}

    return {
        "general": {
            "device_ip": device["device_ip"],
            "device_udp_port": int(device["device_udp_port"]),
            "device_ble_mac": device.get("device_ble_mac", "") or "",
            "device_type": device.get("device_type", "") or "",
            "mqtt_host": mqtt_host,
            "mqtt_port": int(mqtt_port),
            "mqtt_username": mqtt_username,
            "mqtt_password": mqtt_password,
            "mqtt_discovery_prefix": device.get("mqtt_discovery_prefix", "homeassistant"),
            "mqtt_base_topic": device.get("mqtt_base_topic", "Marstek-Bridge-Control"),
            "mqtt_suggested_area": device.get("mqtt_suggested_area", "Marstek"),
        },
        "status_polling": {
            "bat_status_interval_s": int(scan.get("bat_status_interval_s", 3600)),
            "es_mode_interval_s": int(scan.get("es_mode_interval_s", 900)),
            "es_mode_enabled": bool(scan.get("es_mode_enabled", True)),
            "es_status_interval_s": int(scan.get("es_status_interval_s", 300)),
            "pv_enabled": bool(scan.get("pv_enabled", False)),
            "pv_status_interval_s": int(scan.get("pv_status_interval_s", 300)),
        },
        "passive_mode": {
            "power": float(passive.get("power", 800)),
            "cd_time": int(passive.get("cd_time", 60)),
            "max_cd_time": int(passive.get("max_cd_time", 3600)),
        },
        "controller": {
            "deadzone_w": float(ctrl.get("deadzone_w", 40)),
            "min_setpoint_change_w": float(ctrl.get("min_setpoint_change_w", 50)),
            "max_step_w": float(ctrl.get("max_step_w", 125)),
            "min_output_w": float(ctrl.get("min_output_w", -1500)),
            "max_output_w": float(ctrl.get("max_output_w", 800)),
            "min_send_interval_s": float(ctrl.get("min_send_interval_s", 30)),
            "step_gain": float(ctrl.get("step_gain", 1.0)),
            "zero_crossing_hysteresis_w": float(ctrl.get("zero_crossing_hysteresis_w", 0.0)),
            "idle_soc_threshold": float(ctrl.get("idle_soc_threshold", 5.0)),
            "idle_soc_resume_margin": float(ctrl.get("idle_soc_resume_margin", 3.0)),
        },
        "message_settings": {
            "max_retry": int(msg.get("max_retry", 3)),
            "timeout_s": float(msg.get("timeout_s", 1.0)),
            "escalate_on_failure": bool(msg.get("escalate_on_failure", True)),
            "min_inter_message_delay_s": float(msg.get("min_inter_message_delay_s", 2.0)),
        },
        "init": {
            "base_timeout_s": float(init.get("base_timeout_s", 2.0)),
            "timeout_increment_s": float(init.get("timeout_increment_s", 10.0)),
            "max_retries": int(init.get("max_retries", 4)),
            "inter_command_delay_s": float(init.get("inter_command_delay_s", 10.0)),
        },
        "dod": {"startup_value": int(dod.get("startup_value", 88))},
        "led": {"startup_state": int(extra.get("led_startup_state", 0))},
        "ble_block": {"startup_enable": int(extra.get("ble_block_startup_enable", 0))},
        "shelly": {
            "power_topic": ctrl.get("shelly_power_topic", "") or "",
        },
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
