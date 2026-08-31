"""
config.py

Laden, Validieren und Persistieren der Add-on-Konfiguration (YAML).

Wird u.a. verwendet, um waehrend der Erstinitialisierung ermittelte Werte
(device_ble_mac, device_type) dauerhaft in die Datei zurueckzuschreiben,
damit spaetere Starts sie nicht erneut per Broadcast/BLE ermitteln muessen.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Optional, Union

import yaml

logger = logging.getLogger("marstek.config")


DEFAULT_CONFIG: dict = {
    # ── Allgemein / Verbindung ──────────────────────────────────────────
    "general": {
        "device_ip": "192.168.0.45",
        "device_udp_port": 30000,
        "device_ble_mac": "",   # leer -> wird bei Init via BLE.GetStatus ermittelt & persistiert
        "device_type": "",      # leer -> wird bei Init via Marstek.GetDevice ermittelt & persistiert
        "mqtt_host": "core-mosquitto",
        "mqtt_port": 1883,
        "mqtt_username": "mqtt-marstek",
        "mqtt_password": "mqtt-marstek",
        "mqtt_discovery_prefix": "homeassistant",
        "mqtt_base_topic": "Marstek-Bridge-Control",
        "mqtt_suggested_area": "Marstek",
    },
    # ── Abtastrate fuer Status-Abfragen ──────────────────────────────────
    "status_polling": {
        "bat_status_interval_s": 3600,   # 60 min
        "es_mode_interval_s": 900,       # 15 min
        "es_mode_enabled": True,         # periodisches ES.GetMode-Polling ein/aus.
                                          # Betrifft NUR den Poll-Zyklus, NICHT die
                                          # Init-Sequenz (dort bleibt ES.GetMode
                                          # immer bestehen, siehe startup.py).
        "es_status_interval_s": 300,     # 5 min
        "pv_enabled": False,             # PV.GetStatus ein/aus - betrifft SOWOHL
                                          # Init-Sequenz als auch periodisches
                                          # Polling (im Gegensatz zu es_mode_enabled).
                                          # Default aus, da laut API-Doku nur
                                          # Venus D/Venus A PV.GetStatus unterstuetzen,
                                          # Venus C/E NICHT.
        "pv_status_interval_s": 300,     # 5 min
    },
    # ── Passive-Mode Einstellungen (Startwerte; zur Laufzeit ueber die
    #    HA-Number-Entities passive_default_power/passive_cd_time aenderbar,
    #    siehe bridge.py) ──────────────────────────────────────────────────
    "passive_mode": {
        "power": 800,     # Start-Deckel fuer die max. Entlade-/Einspeiseleistung
                          # (= max_output_w-Default: standardmaessig KEINE
                          # zusaetzliche Einschraenkung ueber den Controller
                          # hinaus; z.B. per HA-Automatisierung SOC-abhaengig
                          # absenken, damit der Akku nicht zu schnell leerlaeuft)
        "cd_time": 60,     # Nachlaufzeit/Countdown, mit jedem Passive-Kommando mitgesendet
        "max_cd_time": 3600,
    },
    # ── Regelungsparameter (Passive-Mode Controller) ─────────────────────
    "controller": {
        "deadzone_w": 40,
        "min_setpoint_change_w": 50,
        "max_step_w": 125,
        "min_output_w": -1500,   # harte Ladegrenze (negativ = Laden aus dem Netz)
        "max_output_w": 800,     # harte Einspeisegrenze - Marstek koennte mehr,
                                 # das ist aktuell eine bewusste Zusatzbegrenzung
        "min_send_interval_s": 30,
        "step_gain": 1.0,                # proportionaler Schrittfaktor, siehe
                                          # passive_controller.py. 1.0 = alter
                                          # fester Schrittbegrenzer (rueckwaertskompatibel).
        "zero_crossing_hysteresis_w": 0.0,  # Schwelle fuer einen Vorzeichenwechsel
                                             # (Laden<->Entladen). 0 = deaktiviert
                                             # (rueckwaertskompatibel).
        "idle_soc_threshold": 5.0,       # SOC-Schwelle [%], unter der die automatische
                                          # Passive-Regelung pausiert (kein Senden mehr,
                                          # cd_time laeuft ab -> Geraet faellt in Idle).
                                          # SOC-Quelle: der jeweils zuletzt aktualisierte
                                          # Wert aus Bat.GetStatus.soc ODER
                                          # ES.GetStatus.bat_soc (unterschiedliche
                                          # Poll-Intervalle moeglich).
        "idle_soc_resume_margin": 3.0,   # Hysterese [%]: Regelung startet erst wieder,
                                          # wenn SOC >= idle_soc_threshold + dieser Wert
                                          # (verhindert Aufflattern an der Schwelle).
    },
    # ── Message Settings (laufender Poll-/Control-Betrieb) ───────────────
    "message_settings": {
        "max_retry": 3,
        "timeout_s": 1.0,
        "escalate_on_failure": True,  # unabhaengig von max_retry: ob ein nach
                                      # allen Versuchen weiterhin fehlgeschlagenes
                                      # Kommando zu Communication-Fail/Watchdog
                                      # eskaliert. Betrifft NICHT die Init-Sequenz
                                      # (dort wird immer eskaliert).
        "min_inter_message_delay_s": 2.0,  # Mindestabstand zw. Nachrichten,
                                           # egal welcher Kategorie (0 = aus).
                                           # Control-Kommandos werden davon
                                           # NIE aufgehalten, siehe udp_client.py.
    },
    # ── Initphase Settings ────────────────────────────────────────────────
    "init": {
        "base_timeout_s": 2.0,
        "timeout_increment_s": 10.0,
        "max_retries": 4,
        "inter_command_delay_s": 10.0,  # Pause zwischen einzelnen Init-Kommandos
    },
    # ── DOD Setting (Depth of Discharge) ─────────────────────────────────
    "dod": {
        "startup_value": 88,
    },
    "led": {
        "startup_state": 0,   # 0 = aus
    },
    "ble_block": {
        "startup_enable": 0,  # lt. API: 0 = enable (Advertising aktiv), 1 = disable
    },
    "shelly": {
        "power_topic": "",  # z.B. "shellies/shellyem/emeter/0/power" - leer = deaktiviert
    },
}


class ConfigError(Exception):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class MarstekConfig:
    """Duenner Wrapper um ein verschachteltes dict: Validierung, bequemer
    Zugriff via get(*keys) und persistentes Aktualisieren einzelner Werte."""

    def __init__(self, data: dict, path: Optional[Path] = None):
        self._data = data
        self._path = path

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Union[str, Path]) -> "MarstekConfig":
        path = Path(path)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                user_data = yaml.safe_load(f) or {}
        else:
            logger.warning("Konfigurationsdatei %s existiert nicht, verwende Defaults", path)
            user_data = {}
        data = _deep_merge(DEFAULT_CONFIG, user_data)
        cfg = cls(data, path)
        cfg.validate()
        return cfg

    @classmethod
    def from_dict(cls, overrides: Optional[dict] = None, path: Optional[Path] = None) -> "MarstekConfig":
        """Fuer Tests: Config direkt aus einem (Teil-)dict bauen, ueber Defaults gemerged."""
        data = _deep_merge(DEFAULT_CONFIG, overrides or {})
        cfg = cls(data, path)
        cfg.validate()
        return cfg

    def save(self, path: Optional[Union[str, Path]] = None) -> None:
        target = Path(path) if path else self._path
        if target is None:
            raise ConfigError("Kein Pfad zum Speichern angegeben (weder beim Laden noch hier)")
        with open(target, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)

    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        g = self._data["general"]
        if not g.get("device_ip"):
            raise ConfigError("general.device_ip darf nicht leer sein")
        if not (0 < int(g.get("device_udp_port", 0)) <= 65535):
            raise ConfigError("general.device_udp_port muss zwischen 1 und 65535 liegen")

        ms = self._data["message_settings"]
        if not (0 <= int(ms["max_retry"]) <= 10):
            raise ConfigError("message_settings.max_retry muss zwischen 0 und 10 liegen")
        if not (0 <= float(ms.get("min_inter_message_delay_s", 0)) <= 30):
            raise ConfigError("message_settings.min_inter_message_delay_s muss zwischen 0 und 30 liegen")

        ctrl = self._data["controller"]
        if not (0 <= float(ctrl["min_send_interval_s"]) <= 60):
            raise ConfigError("controller.min_send_interval_s muss zwischen 0 und 60 liegen")
        if float(ctrl["min_output_w"]) >= float(ctrl["max_output_w"]):
            raise ConfigError("controller.min_output_w muss kleiner als max_output_w sein")
        if not (0 <= float(ctrl.get("idle_soc_threshold", 5.0)) <= 100):
            raise ConfigError("controller.idle_soc_threshold muss zwischen 0 und 100 liegen")
        if not (0 <= float(ctrl.get("idle_soc_resume_margin", 3.0)) <= 100):
            raise ConfigError("controller.idle_soc_resume_margin muss zwischen 0 und 100 liegen")
        if not (0.0 < float(ctrl.get("step_gain", 1.0)) <= 1.0):
            raise ConfigError("controller.step_gain muss zwischen > 0 und <= 1 liegen")
        if float(ctrl.get("zero_crossing_hysteresis_w", 0.0)) < 0:
            raise ConfigError("controller.zero_crossing_hysteresis_w darf nicht negativ sein")

        pm = self._data["passive_mode"]
        if not (0 < int(pm["cd_time"]) <= int(pm["max_cd_time"])):
            raise ConfigError("passive_mode.cd_time muss zwischen 1 und max_cd_time liegen")
        if int(pm["max_cd_time"]) > 3600:
            raise ConfigError("passive_mode.max_cd_time darf 3600 nicht ueberschreiten (Geraetevorgabe)")
        if float(pm["power"]) < 0:
            raise ConfigError(
                "passive_mode.power darf nicht negativ sein - es ist ein Deckel fuer die maximale "
                "Entlade-/Einspeiseleistung, keine vorzeichenbehaftete Sollwertvorgabe (0 ist gueltig "
                "und bedeutet: Entladen im Passive-Mode vollstaendig sperren)"
            )


        dod = self._data["dod"]["startup_value"]
        if not (30 <= int(dod) <= 88):
            raise ConfigError("dod.startup_value muss zwischen 30 und 88 liegen")

    # ------------------------------------------------------------------ #
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def set_and_persist(self, *keys: str, value: Any) -> None:
        """Setzt einen Wert im geladenen Config-dict UND schreibt die
        gesamte Datei sofort neu (fuer ble_mac/device_type nach Discovery)."""
        node = self._data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
        if self._path is not None:
            self.save()
            logger.info("Konfiguration aktualisiert und gespeichert: %s = %s", ".".join(keys), value)
        else:
            logger.debug("Konfiguration aktualisiert (nicht persistiert, kein Pfad bekannt): %s = %s",
                         ".".join(keys), value)

    @property
    def raw(self) -> dict:
        return self._data
