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
from input_averager import InputAverager
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

# Gleicher dedizierter Logger wie in passive_controller.py - siehe dort fuer
# die Begruendung, warum dieser separat vom allgemeinen log_level ist.
ctrl_logger = logging.getLogger("marstek.control_logic")


class MarstekBridge:
    def __init__(self, cfg: MarstekConfig, *, debug_control_logic: bool = False):
        self.cfg = cfg
        self.udp: Optional[MarstekUDPClient] = None
        self.mqtt: Optional[HAMqttBridge] = None
        self.bundle: Optional[EntityBundle] = None
        self.passive_ctrl: Optional[PassiveController] = None
        self.shelly_averager: Optional[InputAverager] = None
        # Statisch konfigurierter Grundzustand des ControlLogic-Loggers
        # (aus logging_settings.debug_control_logic). Beim Verlassen des
        # Passive-Mode wird dahin zurueckgekehrt (siehe _handle_energy_mode).
        self._static_debug_control_logic = debug_control_logic

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
            min_inter_message_delay_s=msg_cfg.get("min_inter_message_delay_s", 0.0),
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
        # Zur Laufzeit ueber HA aenderbare Werte (z.B. SOC-abhaengige
        # Automatisierung fuer den Entlade-Deckel). Startwerte kommen aus der
        # Config, werden danach aber NICHT mehr aus self.cfg nachgelesen -
        # einzige Quelle der Wahrheit ist ab jetzt dieser In-Memory-Zustand.
        self._passive_power_cap = float(pm_cfg["power"])
        self._passive_cd_time = int(pm_cfg["cd_time"])
        self.passive_ctrl.set_discharge_cap(self._passive_power_cap)
        self.passive_ctrl.set_cd_time(self._passive_cd_time)

        shelly_cfg = self.cfg.raw["shelly"]
        self._shelly_debounce_time_s = float(shelly_cfg.get("debounce_time_s", 0.0))
        self.shelly_averager = InputAverager(window_s=self._shelly_debounce_time_s)

        await self._publish_startup_values(startup_result)

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
        await self.mqtt.publish_state(e["shelly_debounce_time_s"], self._shelly_debounce_time_s)
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
        # Erst die volle Abtastzeit abwarten, dann erst pollen: die Werte
        # wurden ja gerade erst waehrend der Init-Sequenz frisch abgefragt,
        # ein sofortiger erneuter Poll direkt nach dem Start waere unnoetig.
        while not self._closing:
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                raise
            if self._closing:
                break
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
            elif entity.object_id == "passive_default_power":
                await self._handle_passive_power_cap(payload)
            elif entity.object_id == "passive_cd_time":
                await self._handle_passive_cd_time(payload)
            elif entity.object_id == "shelly_debounce_time_s":
                await self._handle_shelly_debounce_time(payload)
            elif entity.object_id == "passive_resend":
                await self._handle_passive_resend()
            else:
                logger.warning("Kein Handler fuer Kommando auf '%s' registriert", entity.object_id)
        except MarstekCommunicationError:
            logger.error("Kommando fuer '%s' konnte wegen Kommunikationsfehler nicht gesendet werden",
                         entity.object_id)
        except MarstekDeviceError as exc:
            logger.error("Kommando fuer '%s' vom Geraet abgelehnt: %s", entity.object_id, exc)

    async def _handle_passive_power_cap(self, payload: str) -> None:
        """Aendert den dynamischen Entlade-Deckel live, z.B. per SOC-abhaengiger
        HA-Automatisierung. Wirkt SOFORT auf den automatischen Regler
        (naechster Shelly-getriebener update()-Aufruf) UND auf den naechsten
        manuellen Wechsel in den Passive-Mode ueber energy_mode."""
        value = float(payload)
        if value < 0:
            logger.warning("Negativer Wert fuer passive_default_power (%s) ignoriert", payload)
            return
        self._passive_power_cap = value
        self.passive_ctrl.set_discharge_cap(value)
        await self.mqtt.publish_state(self.bundle.entities["passive_default_power"], value)

    async def _handle_passive_cd_time(self, payload: str) -> None:
        value = int(float(payload))
        self._passive_cd_time = value
        self.passive_ctrl.set_cd_time(value)
        await self.mqtt.publish_state(self.bundle.entities["passive_cd_time"], value)

    async def _handle_shelly_debounce_time(self, payload: str) -> None:
        """Aendert das Mittelungsfenster fuer die Shelly-Entprellung live.
        Wirkt sofort auf den naechsten eingehenden Messwert."""
        value = float(payload)
        if value < 0:
            logger.warning("Negativer Wert fuer shelly_debounce_time_s (%s) ignoriert", payload)
            return
        self._shelly_debounce_time_s = value
        self.shelly_averager.set_window(value)
        await self.mqtt.publish_state(self.bundle.entities["shelly_debounce_time_s"], value)

    async def _handle_passive_resend(self) -> None:
        """Sendet das aktuelle Passive-Kommando erneut, ohne den Modus zu
        wechseln. Loest denselben Codepfad wie das (Neu-)Auswaehlen von
        'Passive' im Dropdown aus - inkl. Countdown-Reset (nach bestaetigtem
        Erfolg), Entprellungs-Reset und automatischer ControlLogic-Debug-
        Aktivierung. Grund: HA's select-Entity feuert beim erneuten
        Auswaehlen desselben, bereits aktiven Wertes oft keine neue
        MQTT-Nachricht - dieser Button umgeht das zuverlaessig."""
        await self._handle_energy_mode("Passive")

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
            self._leave_passive_mode()
        elif mode == "AI":
            config = {"mode": "AI", "ai_cfg": {"enable": 1}}
            self._leave_passive_mode()
        elif mode == "Ups":
            config = {"mode": "Ups", "ups_cfg": {"enable": 1}}
            self._leave_passive_mode()
        elif mode == "Passive":
            # Live-Werte verwenden (ueber HA per passive_default_power/
            # passive_cd_time aenderbar), NICHT die statische Config -
            # sonst haette eine Aenderung in HA keine Wirkung (ehemaliger Bug).
            power = self._passive_power_cap
            cd_time = self._passive_cd_time
            config = {"mode": "Passive", "passive_cfg": {"power": int(power), "cd_time": int(cd_time)}}
            # Entprellungs-Fenster bei jedem (erneuten) manuellen Wechsel in
            # den Passive-Mode zuruecksetzen, damit alte Samples aus einer
            # vorherigen Phase nicht die erste Mittelung verfaelschen.
            if self.shelly_averager is not None:
                self.shelly_averager.reset()
            self._enter_passive_mode()
        else:
            logger.warning("Unbekannter Energy-Mode '%s' ignoriert (Manual wird bewusst nicht unterstuetzt)", mode)
            return

        result = await self.udp.send_control("ES.SetMode", {"id": 0, "config": config})
        if result.get("set_result"):
            await self.mqtt.publish_state(self.bundle.entities["energy_mode"], mode)
            if mode == "Passive":
                # Countdown erst NACH bestaetigtem Erfolg zuruecksetzen (nicht
                # vorher optimistisch) - sonst wuerde die Anzeige neu starten,
                # obwohl das Kommando das Geraet nie erreicht hat.
                self._start_countdown(cd_time)

    def _enter_passive_mode(self) -> None:
        """Schaltet den ControlLogic-Debug-Logger automatisch auf DEBUG,
        sobald manuell in den Passive-Mode gewechselt wird - unabhaengig von
        der statischen Config-Option 'debug_control_logic'. Damit laesst
        sich sofort pruefen, ob ueberhaupt Shelly-Nachrichten ankommen,
        ohne vorher extra die Config aendern und neu starten zu muessen."""
        ctrl_logger.setLevel(logging.DEBUG)
        topic = self.cfg.get("shelly", "power_topic", default="")
        if topic:
            ctrl_logger.info(
                "Passive-Mode aktiviert - ControlLogic-Debugging automatisch eingeschaltet. "
                "Shelly-Topic: '%s' (warte auf eingehende Nachrichten)",
                topic, extra={"category": "CONTROLLOGIC"},
            )
        else:
            ctrl_logger.warning(
                "Passive-Mode aktiviert, aber KEIN shelly_power_topic konfiguriert - "
                "die automatische Regelung ist inaktiv, es ist nur manuelle Steuerung ueber HA moeglich.",
                extra={"category": "CONTROLLOGIC"},
            )

    def _leave_passive_mode(self) -> None:
        """Setzt den ControlLogic-Logger beim Verlassen des Passive-Mode auf
        den statisch konfigurierten Grundzustand zurueck (Option
        'debug_control_logic'), statt dauerhaft auf DEBUG stehen zu bleiben."""
        ctrl_logger.setLevel(logging.DEBUG if self._static_debug_control_logic else logging.WARNING)

    # ------------------------------------------------------------------ #
    # Passive-Regler <- externe Leistungsmessung (z.B. Shelly)
    # ------------------------------------------------------------------ #

    async def _on_shelly_power(self, topic: str, payload: str) -> None:
        try:
            raw_power = float(payload)
        except ValueError:
            logger.warning("Ungueltiger Leistungswert auf %s: %r", topic, payload)
            return

        ctrl_logger.debug("Shelly-Eingang (roh): %.1f W", raw_power,
                           extra={"category": "CONTROLLOGIC"})

        # Entprellung: gleitender Mittelwert ueber das konfigurierte Fenster,
        # BEVOR der Wert in die Regellogik einfliesst.
        smoothed_power = self.shelly_averager.add(raw_power)
        ctrl_logger.debug(
            "Shelly-Eingang (entprellt, Fenster=%.0fs, %d Samples): %.1f W",
            self.shelly_averager.window_s, self.shelly_averager.sample_count(), smoothed_power,
            extra={"category": "CONTROLLOGIC"},
        )

        # Zielwert fuer den Marstek = -Netzleistung (Netzbezug/-einspeisung auf 0 regeln)
        target = -smoothed_power
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
