"""
bridge.py

Verdrahtet alle bisherigen Bausteine zu einer lauffaehigen Anwendung:

    config.py + startup.py + entities.py + udp_client.py + mqtt_ha.py
    + passive_controller.py

Ablauf von MarstekBridge.start():
    1. UDP-Client verbinden
    2. MQTT-Bridge verbinden (Availability "online")
    3. Init-Sequenz durchlaufen (startup.py) - liefert ble_mac/device_type
    4. Entities bauen (entities.py) & bei HA registrieren (Discovery)
    5. Init-Werte einmalig veroeffentlichen
    6. Passive-Regler aufsetzen
    7. Poll-Loops fuer Bat.GetStatus / ES.GetMode / ES.GetStatus starten
    8. Optional: Shelly-Leistungsmessung abonnieren -> Passive-Regler -> ES.SetMode

HA -> Bridge Kommandos (Select/Number/Switch) werden ueber den
HAMqttBridge-Callback hereingereicht und auf die passenden
udp_client.send_control(...)-Aufrufe abgebildet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from config import MarstekConfig
from entities import EntityBundle, build_entities
from mqtt_ha import HAEntity, HAMqttBridge
from passive_controller import PassiveController, PassiveControllerConfig
from startup import run_startup_sequence
from udp_client import (
    InitRetryPolicy,
    MarstekCommunicationError,
    MarstekDeviceError,
    MarstekUDPClient,
    RuntimeRetryPolicy,
)

logger = logging.getLogger("marstek.bridge")


class MarstekBridge:
    def __init__(self, cfg: MarstekConfig):
        self.cfg = cfg
        self.udp: Optional[MarstekUDPClient] = None
        self.mqtt: Optional[HAMqttBridge] = None
        self.bundle: Optional[EntityBundle] = None
        self.passive_ctrl: Optional[PassiveController] = None

        self._poll_tasks: list[asyncio.Task] = []
        self._countdown_task: Optional[asyncio.Task] = None
        self._passive_cd_deadline: Optional[float] = None
        self._closing = False

        self.ble_mac: Optional[str] = None
        self.device_type: Optional[str] = None
        self.shutdown_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        g = self.cfg.raw["general"]
        init_cfg = self.cfg.raw["init"]
        msg_cfg = self.cfg.raw["message_settings"]

        self.udp = MarstekUDPClient(
            g["device_ip"], g["device_udp_port"],
            init_policy=InitRetryPolicy(
                base_timeout_s=init_cfg["base_timeout_s"],
                timeout_increment_s=init_cfg["timeout_increment_s"],
                max_retries=init_cfg["max_retries"],
            ),
            runtime_policy=RuntimeRetryPolicy(
                timeout_s=msg_cfg["timeout_s"], max_retries=msg_cfg["max_retry"],
            ),
            on_comm_state_change=self._on_comm_state_change,
            on_comm_fail_watchdog=self._on_watchdog,
        )
        await self.udp.connect()

        startup_result = await run_startup_sequence(self.udp, self.cfg)
        self.ble_mac = startup_result.ble_mac
        self.device_type = startup_result.device_type

        self.mqtt = HAMqttBridge(
            g["mqtt_host"], g["mqtt_port"],
            username=g.get("mqtt_username") or None,
            password=g.get("mqtt_password") or None,
            discovery_prefix=g["mqtt_discovery_prefix"],
            base_topic=g["mqtt_base_topic"],
            node_id=f"marstek_{self.ble_mac}",
        )
        await self.mqtt.connect()
        self.mqtt.set_command_callback(self._on_ha_command)

        self.bundle = build_entities(self.cfg, self.ble_mac, self.device_type)
        for entity in self.bundle.entities.values():
            await self.mqtt.register_entity(entity)

        await self._publish_startup_values(startup_result)

        ctrl_cfg = self.cfg.raw["controller"]
        pm_cfg = self.cfg.raw["passive_mode"]
        self.passive_ctrl = PassiveController(PassiveControllerConfig(
            deadzone_w=ctrl_cfg["deadzone_w"],
            min_setpoint_change_w=ctrl_cfg["min_setpoint_change_w"],
            max_step_w=ctrl_cfg["max_step_w"],
            min_output_w=ctrl_cfg["min_output_w"],
            max_output_w=ctrl_cfg["max_output_w"],
            min_send_interval_s=ctrl_cfg["min_send_interval_s"],
            default_cd_time_s=pm_cfg["cd_time"],
            max_cd_time_s=pm_cfg["max_cd_time"],
        ))

        await self.mqtt.publish_state(self.bundle.entities["udp_connection"], self.udp.comm_established)
        await self.mqtt.publish_state(self.bundle.entities["communication_fail"], self.udp.comm_fail)

        sp = self.cfg.raw["status_polling"]
        self._poll_tasks = [
            asyncio.create_task(self._poll_loop("Bat.GetStatus", self._on_battery_status,
                                                 sp["bat_status_interval_s"])),
            asyncio.create_task(self._poll_loop("ES.GetMode", self._on_es_mode,
                                                 sp["es_mode_interval_s"])),
            asyncio.create_task(self._poll_loop("ES.GetStatus", self._on_es_status,
                                                 sp["es_status_interval_s"])),
        ]
        self._countdown_task = asyncio.create_task(self._countdown_loop())

        shelly_topic = self.cfg.raw["shelly"]["power_topic"]
        if shelly_topic:
            await self.mqtt.subscribe_raw(shelly_topic, self._on_shelly_power)
            logger.info("Shelly-Leistungsmessung abonniert: %s", shelly_topic)

        # ALLERLETZTER Schritt: signalisiert uebergeordneten HA-Automatisierungen,
        # dass die Initialisierung abgeschlossen ist und der Pollzyklus beginnt.
        # Bewusst getrennt von 'udp_connection' (das nur die UDP-Verbindungs-
        # guete abbildet und waehrend des Betriebs unabhaengig davon kippen kann).
        await self.mqtt.publish_state(self.bundle.entities["system_ready"], True)

        logger.info("Marstek-Bridge vollstaendig gestartet (device_type=%s, ble_mac=%s)",
                    self.device_type, self.ble_mac)

    async def stop(self) -> None:
        self._closing = True
        for t in self._poll_tasks:
            t.cancel()
        if self._countdown_task:
            self._countdown_task.cancel()
        for t in [*self._poll_tasks, self._countdown_task]:
            if t is None:
                continue
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self.mqtt and self.bundle:
            try:
                await self.mqtt.publish_state(self.bundle.entities["system_ready"], False)
            except Exception:
                logger.debug("Konnte 'system_ready=False' beim Stop nicht mehr publizieren", exc_info=True)
        if self.mqtt:
            await self.mqtt.close()
        if self.udp:
            await self.udp.close()

    # ------------------------------------------------------------------ #
    # Init-Werte einmalig veroeffentlichen
    # ------------------------------------------------------------------ #

    async def _publish_startup_values(self, r) -> None:
        e = self.bundle.entities
        await self.mqtt.publish_state(e["device_model"], r.device_type)
        if r.device_ver is not None:
            await self.mqtt.publish_state(e["device_fw_version"], r.device_ver)
        if r.device_ip:
            await self.mqtt.publish_state(e["wifi_ip"], r.device_ip)
        if r.wifi_name:
            await self.mqtt.publish_state(e["wifi_ssid"], r.wifi_name)
        if r.wifi_mac:
            await self.mqtt.publish_state(e["wifi_mac"], r.wifi_mac)
        await self.mqtt.publish_state(e["ble_mac"], r.ble_mac)
        if r.ble_state:
            await self.mqtt.publish_state(e["ble_state"], r.ble_state)
        if isinstance(r.wifi_status, dict) and "rssi" in r.wifi_status:
            await self.mqtt.publish_state(e["wifi_rssi"], r.wifi_status["rssi"])

        await self._publish_mapped(r.battery_status, self.bundle.field_map_battery)
        await self._publish_mapped(r.es_status, self.bundle.field_map_es_status)
        await self._publish_mapped(r.es_mode, self.bundle.field_map_es_mode)

        await self.mqtt.publish_state(e["dod_value"], self.cfg.get("dod", "startup_value", default=88))
        await self.mqtt.publish_state(e["passive_default_power"],
                                       self.cfg.get("passive_mode", "power", default=50))
        await self.mqtt.publish_state(e["passive_cd_time"],
                                       self.cfg.get("passive_mode", "cd_time", default=60))
        ble_broadcast_on = int(self.cfg.get("ble_block", "startup_enable", default=0)) == 0
        await self.mqtt.publish_state(e["ble_broadcast"], ble_broadcast_on)
        led_on = int(self.cfg.get("led", "startup_state", default=0)) == 1
        await self.mqtt.publish_state(e["led_ctrl"], led_on)

    async def _publish_mapped(self, result: dict, field_map: dict) -> None:
        if not isinstance(result, dict):
            return
        for field_name, object_id in field_map.items():
            if field_name in result and result[field_name] is not None:
                await self.mqtt.publish_state(self.bundle.entities[object_id], result[field_name])

    # ------------------------------------------------------------------ #
    # Poll-Loops (Status-Abfragen, niedrige Prioritaet gegenueber Control)
    # ------------------------------------------------------------------ #

    async def _poll_loop(self, method: str, handler, interval_s: float) -> None:
        while not self._closing:
            try:
                result = await self.udp.send_status(method, {"id": 0})
                await handler(result)
            except MarstekCommunicationError:
                logger.warning("Poll %s fehlgeschlagen (Communication Fail)", method)
            except MarstekDeviceError as exc:
                logger.error("Poll %s: Geraetefehler %s", method, exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unerwarteter Fehler beim Poll von %s", method)
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                raise

    async def _on_battery_status(self, result: dict) -> None:
        await self._publish_mapped(result, self.bundle.field_map_battery)

    async def _on_es_status(self, result: dict) -> None:
        await self._publish_mapped(result, self.bundle.field_map_es_status)

    async def _on_es_mode(self, result: dict) -> None:
        await self._publish_mapped(result, self.bundle.field_map_es_mode)

    # ------------------------------------------------------------------ #
    # HA -> Bridge Kommandos
    # ------------------------------------------------------------------ #

    async def _on_ha_command(self, entity: HAEntity, payload: str) -> None:
        try:
            if entity.object_id == "dod_value":
                await self._handle_dod(payload)
            elif entity.object_id == "ble_broadcast":
                await self._handle_ble_broadcast(payload)
            elif entity.object_id == "led_ctrl":
                await self._handle_led_ctrl(payload)
            elif entity.object_id == "energy_mode":
                await self._handle_energy_mode(payload)
            elif entity.object_id in ("passive_default_power", "passive_cd_time"):
                # Wirkt erst beim naechsten manuellen Wechsel in den Passive-Mode;
                # hier nur den neuen Wert an HA zurueckspiegeln.
                await self.mqtt.publish_state(entity, payload)
            else:
                logger.warning("Kein Handler fuer Kommando auf '%s' registriert", entity.object_id)
        except MarstekCommunicationError:
            logger.error("Kommando fuer '%s' konnte wegen Kommunikationsfehler nicht gesendet werden",
                         entity.object_id)
        except MarstekDeviceError as exc:
            logger.error("Kommando fuer '%s' vom Geraet abgelehnt: %s", entity.object_id, exc)

    async def _handle_dod(self, payload: str) -> None:
        value = int(float(payload))
        result = await self.udp.send_control("DOD.SET", {"value": value})
        if result.get("set_result"):
            await self.mqtt.publish_state(self.bundle.entities["dod_value"], value)

    async def _handle_ble_broadcast(self, payload: str) -> None:
        enable_flag = 0 if payload == "ON" else 1  # API: 0 = enable (Broadcast aktiv), 1 = disable
        result = await self.udp.send_control("Ble.Adv", {"enable": enable_flag})
        if result.get("set_result"):
            await self.mqtt.publish_state(self.bundle.entities["ble_broadcast"], payload == "ON")

    async def _handle_led_ctrl(self, payload: str) -> None:
        state = 1 if payload == "ON" else 0
        result = await self.udp.send_control("Led.Ctrl", {"state": state})
        if result.get("set_result"):
            await self.mqtt.publish_state(self.bundle.entities["led_ctrl"], payload == "ON")

    async def _handle_energy_mode(self, payload: str) -> None:
        mode = payload
        if mode == "Auto":
            config = {"mode": "Auto", "auto_cfg": {"enable": 1}}
        elif mode == "AI":
            config = {"mode": "AI", "ai_cfg": {"enable": 1}}
        elif mode == "Ups":
            config = {"mode": "Ups", "ups_cfg": {"enable": 1}}
        elif mode == "Passive":
            power = int(self.cfg.get("passive_mode", "power", default=50))
            cd_time = int(self.cfg.get("passive_mode", "cd_time", default=60))
            config = {"mode": "Passive", "passive_cfg": {"power": power, "cd_time": cd_time}}
            self._start_countdown(cd_time)
        else:
            logger.warning("Unbekannter Energy-Mode '%s' ignoriert (Manual wird bewusst nicht unterstuetzt)", mode)
            return

        result = await self.udp.send_control("ES.SetMode", {"id": 0, "config": config})
        if result.get("set_result"):
            await self.mqtt.publish_state(self.bundle.entities["energy_mode"], mode)

    # ------------------------------------------------------------------ #
    # Passive-Regler <- externe Leistungsmessung (z.B. Shelly)
    # ------------------------------------------------------------------ #

    async def _on_shelly_power(self, topic: str, payload: str) -> None:
        try:
            raw_power = float(payload)
        except ValueError:
            logger.warning("Ungueltiger Leistungswert auf %s: %r", topic, payload)
            return

        # Zielwert fuer den Marstek = -Netzleistung (Netzbezug/-einspeisung auf 0 regeln)
        target = -raw_power
        cmd = self.passive_ctrl.update(target)
        if cmd is None:
            return

        result = await self.udp.send_control(
            "ES.SetMode", {"id": 0, "config": {"mode": "Passive", "passive_cfg": cmd}}
        )
        if result.get("set_result"):
            await self.mqtt.publish_state(self.bundle.entities["passive_last_sent_power"], cmd["power"])
            self._start_countdown(cmd["cd_time"])

    # ------------------------------------------------------------------ #
    # Lokaler Countdown-Tracker (das Geraet liefert cd_time selbst nicht zurueck)
    # ------------------------------------------------------------------ #

    def _start_countdown(self, cd_time_s: int) -> None:
        self._passive_cd_deadline = time.monotonic() + cd_time_s

    async def _countdown_loop(self) -> None:
        while not self._closing:
            if self._passive_cd_deadline is not None:
                remaining = max(0, int(self._passive_cd_deadline - time.monotonic()))
                await self.mqtt.publish_state(self.bundle.entities["passive_cd_time_remaining"], remaining)
                if remaining <= 0:
                    self._passive_cd_deadline = None
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------ #
    # Verbindungsstatus
    # ------------------------------------------------------------------ #

    def _on_comm_state_change(self, established: bool, fail: bool) -> None:
        if self.mqtt and self.bundle:
            asyncio.create_task(
                self.mqtt.publish_state(self.bundle.entities["udp_connection"], established)
            )
            asyncio.create_task(
                self.mqtt.publish_state(self.bundle.entities["communication_fail"], fail)
            )

    async def _on_watchdog(self) -> None:
        logger.critical(
            "WATCHDOG: Kommunikation dauerhaft gestoert (max_retry erreicht) - "
            "Bridge signalisiert Neustartbedarf"
        )
        if self.mqtt and self.bundle:
            try:
                await self.mqtt.publish_state(self.bundle.entities["system_ready"], False)
            except Exception:
                logger.debug("Konnte 'system_ready=False' beim Watchdog nicht publizieren", exc_info=True)
        self._closing = True
        self.shutdown_event.set()
