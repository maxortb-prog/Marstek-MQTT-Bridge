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
from entities import EntityBundle, build_entities, pv_object_ids_and_components
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
        # Statisch konfigurierter Grundzustand des ControlLogic-Loggers
        # (aus logging_settings.debug_control_logic). Beim Verlassen des
        # Passive-Mode wird dahin zurueckgekehrt (siehe _handle_energy_mode).
        self._static_debug_control_logic = debug_control_logic

        self._poll_tasks: list[asyncio.Task] = []
        self._countdown_task: Optional[asyncio.Task] = None
        self._idle_keepalive_task: Optional[asyncio.Task] = None
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
                escalate_on_failure=msg_cfg.get("escalate_on_failure", True),
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

        if not self.cfg.get("status_polling", "pv_enabled", default=False):
            # PV ist deaktiviert -> ggf. zuvor registrierte PV-Entities aus HA
            # entfernen (falls die Option frueher mal aktiv war). Idempotent
            # und guenstig, auch wenn nie eine PV-Entity existiert hat.
            for component, object_id in pv_object_ids_and_components():
                await self.mqtt.remove_entity_discovery(component, object_id)

        ctrl_cfg = self.cfg.raw["controller"]
        pm_cfg = self.cfg.raw["passive_mode"]
        self.passive_ctrl = PassiveController(PassiveControllerConfig(
            deadzone_w=ctrl_cfg["deadzone_w"],
            min_setpoint_change_w=ctrl_cfg["min_setpoint_change_w"],
            max_step_w=ctrl_cfg["max_step_w"],
            min_output_w=ctrl_cfg["min_output_w"],
            max_output_w=ctrl_cfg["max_output_w"],
            min_send_interval_s=ctrl_cfg["min_send_interval_s"],
            step_gain=ctrl_cfg.get("step_gain", 1.0),
            zero_crossing_hysteresis_w=ctrl_cfg.get("zero_crossing_hysteresis_w", 0.0),
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

        # Sollwert-Kontinuitaet ueber einen Neustart hinweg: den waehrend der
        # Init-Sequenz ohnehin schon abgefragten aktuellen Geraetezustand
        # (ES.GetMode.ongrid_power) als Startpunkt fuer die integrale Regelung
        # uebernehmen, statt bei jedem Neustart faelschlich von 0W auszugehen.
        # Sonst wuerde die erste automatische Korrektur nach einem Neustart
        # einen grossen, ungewollten Sprung verursachen (z.B. Geraet laeuft
        # bereits bei 261W, Regler nimmt faelschlich 0W an und springt sofort
        # auf einen stark abweichenden Wert statt sanft anzupassen).
        if isinstance(startup_result.es_mode, dict):
            last_known_output = startup_result.es_mode.get("ongrid_power")
            current_device_mode = startup_result.es_mode.get("mode")
            if last_known_output is not None:
                self.passive_ctrl.state.committed_setpoint_w = float(last_known_output)
                self.passive_ctrl.state.last_sent_setpoint_w = float(last_known_output)
                logger.info(
                    "Passive-Regler: Sollwert-Kontinuitaet nach (Neu-)Start - "
                    "starte mit %.0fW (aus ES.GetMode.ongrid_power) statt 0W",
                    last_known_output,
                )
                if current_device_mode == "Passive":
                    # Das Seeding oben aktualisiert nur den INTERNEN Zustand,
                    # sendet aber nichts ans Geraet - die dortige cd_time laeuft
                    # unbeeinflusst weiter (evtl. schon fast abgelaufen), und
                    # "Passive Countdown Remaining" bliebe uninitialisiert. Nur
                    # wenn das Geraet gerade tatsaechlich im Passive-Mode ist
                    # (kein ungewollter Moduswechsel!), wird der gleiche Wert
                    # jetzt aktiv nochmal gesendet, um die geraeteseitige
                    # cd_time frisch zu setzen und den lokalen Countdown zu
                    # (re-)initialisieren.
                    refresh_power = int(round(last_known_output))
                    if refresh_power == 0:
                        refresh_power = 1
                    try:
                        refresh_result = await self.udp.send_control(
                            "ES.SetMode",
                            {"id": 0, "config": {"mode": "Passive", "passive_cfg": {
                                "power": refresh_power, "cd_time": self._passive_cd_time,
                            }}},
                        )
                    except (MarstekCommunicationError, MarstekDeviceError):
                        logger.exception("cd_time-Auffrischung nach Neustart fehlgeschlagen")
                    else:
                        if refresh_result.get("set_result"):
                            self.passive_ctrl.state.last_send_monotonic = time.monotonic()
                            self._start_countdown(self._passive_cd_time)
                            logger.info(
                                "cd_time nach Neustart aufgefrischt (power=%dW, cd_time=%ds)",
                                refresh_power, self._passive_cd_time,
                            )

        # Idle-bei-niedrigem-SOC: jeweils zuletzt aktualisierter SOC-Wert aus
        # Bat.GetStatus.soc ODER ES.GetStatus.bat_soc (unterschiedliche
        # Poll-Intervalle moeglich - "wer zuerst aktualisiert, gewinnt").
        self._latest_battery_soc: Optional[float] = None
        if isinstance(startup_result.battery_status, dict) and startup_result.battery_status.get("soc") is not None:
            self._latest_battery_soc = float(startup_result.battery_status["soc"])
        elif isinstance(startup_result.es_status, dict) and startup_result.es_status.get("bat_soc") is not None:
            self._latest_battery_soc = float(startup_result.es_status["bat_soc"])
        self._idle_soc_threshold = float(ctrl_cfg.get("idle_soc_threshold", 5.0))
        self._idle_soc_resume_margin = float(ctrl_cfg.get("idle_soc_resume_margin", 3.0))
        self._passive_idle_low_soc = False
        self._idle_keepalive_last_sent: Optional[float] = None

        await self._publish_startup_values(startup_result)

        await self.mqtt.publish_state(self.bundle.entities["udp_connection"], self.udp.comm_established)
        await self.mqtt.publish_state(self.bundle.entities["communication_fail"], self.udp.comm_fail)

        sp = self.cfg.raw["status_polling"]
        self._poll_tasks = [
            asyncio.create_task(self._poll_loop("Bat.GetStatus", self._on_battery_status,
                                                 sp["bat_status_interval_s"])),
            asyncio.create_task(self._poll_loop("ES.GetStatus", self._on_es_status,
                                                 sp["es_status_interval_s"])),
        ]
        if sp.get("es_mode_enabled", True):
            self._poll_tasks.append(
                asyncio.create_task(self._poll_loop("ES.GetMode", self._on_es_mode,
                                                     sp["es_mode_interval_s"]))
            )
        else:
            logger.info("Periodisches ES.GetMode-Polling ist deaktiviert (es_mode_enabled=false). "
                        "Die Init-Sequenz fragt ES.GetMode weiterhin einmalig ab.")
        if sp.get("pv_enabled", False):
            self._poll_tasks.append(
                asyncio.create_task(self._poll_loop("PV.GetStatus", self._on_pv_status,
                                                     sp["pv_status_interval_s"]))
            )
        self._countdown_task = asyncio.create_task(self._countdown_loop())
        self._idle_keepalive_task = asyncio.create_task(self._idle_keepalive_loop())

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
        if self._idle_keepalive_task:
            self._idle_keepalive_task.cancel()
        for t in [*self._poll_tasks, self._countdown_task, self._idle_keepalive_task]:
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
        if r.pv_status is not None:
            await self._publish_mapped(r.pv_status, self.bundle.field_map_pv)
            await self._publish_pv_states(r.pv_status)

        await self.mqtt.publish_state(e["dod_value"], self.cfg.get("dod", "startup_value", default=88))
        await self.mqtt.publish_state(e["passive_default_power"],
                                       self.cfg.get("passive_mode", "power", default=50))
        await self.mqtt.publish_state(e["passive_cd_time"],
                                       self.cfg.get("passive_mode", "cd_time", default=60))
        await self.mqtt.publish_state(e["idle_soc_threshold"], self._idle_soc_threshold)
        await self.mqtt.publish_state(e["passive_idle_low_soc"], self._passive_idle_low_soc)
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

    async def _publish_pv_states(self, result: dict) -> None:
        """Die pvN_state-Felder (1=Work/0=Standby) werden als binary_sensor
        abgebildet - brauchen daher eine explizite bool-Konvertierung statt
        der generischen Zahlen-Weiterleitung in _publish_mapped."""
        if not isinstance(result, dict) or not self.bundle.field_map_pv_state:
            return
        for field_name, object_id in self.bundle.field_map_pv_state.items():
            if field_name in result and result[field_name] is not None:
                await self.mqtt.publish_state(self.bundle.entities[object_id], bool(result[field_name]))

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
        if isinstance(result, dict) and result.get("soc") is not None:
            self._latest_battery_soc = float(result["soc"])

    async def _on_es_status(self, result: dict) -> None:
        await self._publish_mapped(result, self.bundle.field_map_es_status)
        if isinstance(result, dict) and result.get("bat_soc") is not None:
            self._latest_battery_soc = float(result["bat_soc"])

    async def _on_es_mode(self, result: dict) -> None:
        await self._publish_mapped(result, self.bundle.field_map_es_mode)

    async def _on_pv_status(self, result: dict) -> None:
        await self._publish_mapped(result, self.bundle.field_map_pv)
        await self._publish_pv_states(result)

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
            elif entity.object_id == "idle_soc_threshold":
                await self._handle_idle_soc_threshold(payload)
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

    async def _handle_idle_soc_threshold(self, payload: str) -> None:
        """Aendert die SOC-Idle-Schwelle live. Wirkt sofort - ein bereits
        laufender Idle-Zustand wird beim naechsten Shelly-Zyklus neu
        bewertet (siehe _update_idle_state)."""
        value = float(payload)
        if not (0 <= value <= 100):
            logger.warning("Ungueltiger Wert fuer idle_soc_threshold (%s) ignoriert", payload)
            return
        self._idle_soc_threshold = value
        await self.mqtt.publish_state(self.bundle.entities["idle_soc_threshold"], value)

    async def _update_idle_state(self) -> None:
        """Idle-bei-niedrigem-SOC mit Hysterese: pausiert die automatische
        Passive-Regelung (kein Senden mehr, cd_time laeuft auf dem Geraet
        ab -> faellt in Idle), sobald der zuletzt bekannte SOC unter
        idle_soc_threshold faellt. Startet erst wieder, wenn der SOC
        mindestens idle_soc_threshold + idle_soc_resume_margin erreicht
        (verhindert Aufflattern direkt an der Schwelle)."""
        if self._latest_battery_soc is None:
            return
        soc = self._latest_battery_soc

        if not self._passive_idle_low_soc and soc <= self._idle_soc_threshold:
            self._passive_idle_low_soc = True
            logger.warning(
                "Passive-Regelung pausiert: SOC (%.1f%%) <= Idle-Schwelle (%.1f%%) - "
                "cd_time laeuft ab, Geraet faellt in Idle",
                soc, self._idle_soc_threshold,
            )
            await self.mqtt.publish_state(self.bundle.entities["passive_idle_low_soc"], True)
        elif self._passive_idle_low_soc and soc >= (self._idle_soc_threshold + self._idle_soc_resume_margin):
            self._passive_idle_low_soc = False
            logger.info(
                "Passive-Regelung wieder aktiv: SOC (%.1f%%) >= Wiederaufnahme-Schwelle (%.1f%%)",
                soc, self._idle_soc_threshold + self._idle_soc_resume_margin,
            )
            await self.mqtt.publish_state(self.bundle.entities["passive_idle_low_soc"], False)

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

        # Sofortige Idle-Pruefung bei jeder Shelly-Nachricht (fuer schnelle
        # Reaktion), ZUSAETZLICH zum periodischen Hintergrund-Task
        # (_idle_keepalive_loop), der auch ohne Shelly-Nachrichten laeuft.
        await self._update_idle_state()
        if self._passive_idle_low_soc:
            # Waehrend SOC-Idle sendet der eigene Hintergrund-Task
            # (_idle_keepalive_loop) periodisch ein Passive-Kommando bei
            # ~0W - die normale Regelung ist hier bewusst ausgesetzt.
            ctrl_logger.debug(
                "Passive-Regelung pausiert (SOC zu niedrig) - Shelly-Wert %.1f W ignoriert "
                "(Idle-Keepalive laeuft im Hintergrund)",
                raw_power, extra={"category": "CONTROLLOGIC"},
            )
            return

        ctrl_logger.debug("Shelly-Eingang: %.1f W", raw_power,
                           extra={"category": "CONTROLLOGIC"})

        # Zielwert fuer den Marstek: INTEGRALE Regelung, nicht direkte
        # Zielwertvorgabe. Der Shelly misst den TATSAECHLICHEN Netzbezug
        # (positiv) bzw. die Einspeisung (negativ) AN DER MESSSTELLE -
        # inklusive dem, was der Marstek selbst gerade tut. Ein direktes
        # "target = -raw_power" wuerde eine Mitkopplung erzeugen: Laden
        # (negativer Sollwert) erhoeht den gemessenen Netzbezug, was
        # faelschlich als "noch mehr laden noetig" interpretiert wuerde ->
        # Aufschaukeln statt Stabilisierung.
        #
        # Korrekte Herleitung: netzbezug = (last - pv) - aktueller_sollwert
        # (aktueller_sollwert positiv=einspeisen/entladen, negativ=laden).
        # Fuer netzbezug_neu = 0 mit neuem Sollwert gilt:
        #   neuer_sollwert = aktueller_sollwert + netzbezug_gemessen
        # Das ist ein integrierender Regelschritt: der gemessene Fehler wird
        # auf den bestehenden Sollwert aufaddiert, nicht ersetzt.
        current_setpoint = self.passive_ctrl.state.committed_setpoint_w
        if current_setpoint is None:
            current_setpoint = 0.0
        target = current_setpoint + raw_power
        ctrl_logger.debug(
            "Integrale Zielwertberechnung: aktueller Sollwert=%.1f W + Netzbezug=%.1f W -> Ziel=%.1f W",
            current_setpoint, raw_power, target,
            extra={"category": "CONTROLLOGIC"},
        )
        cmd = self.passive_ctrl.update(target)
        if cmd is None:
            return

        try:
            result = await self.udp.send_control(
                "ES.SetMode", {"id": 0, "config": {"mode": "Passive", "passive_cfg": cmd}}
            )
        except (MarstekCommunicationError, MarstekDeviceError):
            logger.warning(
                "Passive-Kommando (power=%dW) konnte nicht gesendet werden - "
                "wird beim naechsten Zyklus erneut versucht",
                cmd["power"],
            )
            return
        if result.get("set_result"):
            await self.mqtt.publish_state(self.bundle.entities["passive_last_sent_power"], cmd["power"])
            self._start_countdown(cmd["cd_time"])

    # ------------------------------------------------------------------ #
    # Idle bei niedrigem SOC (eigener Hintergrund-Task)
    # ------------------------------------------------------------------ #

    async def _idle_keepalive_loop(self) -> None:
        """Laeuft dauerhaft im Hintergrund, unabhaengig vom Shelly-
        Nachrichtenfluss: erkennt SOC-Schwellenuebertritte und haelt das
        Geraet waehrend einer Idle-Phase (SOC zu niedrig) aktiv am Leben -
        mit einem kontinuierlichen Passive-Kommando bei ~0W (alle
        cd_time/2), statt die geraeteseitige cd_time einfach ablaufen zu
        lassen. Reines Verstreichenlassen wuerde laut Rueckmeldung dazu
        fuehren, dass das Geraet die Verbindung/den Passive-Modus komplett
        verliert - stattdessen bleibt der Modus aktiv erhalten, nur ohne
        Lade-/Entladeleistung."""
        while not self._closing:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                raise
            await self._update_idle_state()
            if not self._passive_idle_low_soc:
                self._idle_keepalive_last_sent = None
                continue
            now = time.monotonic()
            half_cd_time = max(5.0, self._passive_cd_time / 2)
            if self._idle_keepalive_last_sent is None or (now - self._idle_keepalive_last_sent) >= half_cd_time:
                await self._send_idle_keepalive()
                self._idle_keepalive_last_sent = now

    async def _send_idle_keepalive(self) -> None:
        config = {"mode": "Passive", "passive_cfg": {"power": 1, "cd_time": self._passive_cd_time}}
        try:
            result = await self.udp.send_control("ES.SetMode", {"id": 0, "config": config})
        except (MarstekCommunicationError, MarstekDeviceError):
            logger.exception("Idle-Keepalive (SOC zu niedrig) konnte nicht gesendet werden")
            return
        if result.get("set_result"):
            logger.info(
                "Idle-Keepalive gesendet (SOC zu niedrig): power~0W cd_time=%ds",
                self._passive_cd_time,
            )
            await self.mqtt.publish_state(self.bundle.entities["passive_last_sent_power"], 1)
            self._start_countdown(self._passive_cd_time)

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
