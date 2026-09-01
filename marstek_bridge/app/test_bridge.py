"""
test_bridge.py

End-to-End-Test der kompletten Verdrahtung: config -> udp_client (gegen
fake_marstek_server) -> startup -> entities -> mqtt_ha (gegen echten lokalen
Mosquitto-Broker) -> passive_controller.

Simuliert Home Assistant durch einen zusaetzlichen aiomqtt-Client, der
Discovery-/State-Topics mitliest und Command-Topics beschreibt.
"""

import asyncio
import json
import logging
import time

import aiomqtt
import pytest

from bridge import MarstekBridge
from config import MarstekConfig
from fake_marstek_server import FakeMarstekServer, MethodBehavior

logging.basicConfig(level=logging.INFO)

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883


def _setup_full_server_behavior(server: FakeMarstekServer):
    server.set_behavior("Marstek.GetDevice", MethodBehavior(
        result={"device": "VenusC", "ver": 111, "wifi_mac": "aabbcc", "wifi_name": "MY_HOME", "ip": "192.168.1.11"}))
    server.set_behavior("Wifi.GetStatus", MethodBehavior(result={"wifi_mac": "aabbcc", "ssid": "MY_HOME", "rssi": -55}))
    server.set_behavior("BLE.GetStatus", MethodBehavior(result={"state": "connect", "ble_mac": "50cf14640fac"}))
    server.set_behavior("Bat.GetStatus", MethodBehavior(
        result={"soc": 90, "charg_flag": True, "dischrg_flag": True, "bat_temp": 25.0,
                "bat_capacity": 256.0, "rated_capacity": 2560.0}))
    server.set_behavior("ES.GetStatus", MethodBehavior(
        result={"bat_soc": 91, "pv_power": 120, "ongrid_power": 0, "offgrid_power": 0}))
    server.set_behavior("ES.GetMode", MethodBehavior(result={"mode": "Auto", "bat_soc": 91}))
    server.set_behavior("DOD.SET", MethodBehavior(result={"set_result": True}))
    server.set_behavior("Ble.Adv", MethodBehavior(result={"set_result": True}))
    server.set_behavior("Led.Ctrl", MethodBehavior(result={"set_result": True}))
    server.set_behavior("ES.SetMode", MethodBehavior(result={"set_result": True}))


def _test_config(tmp_path, device_port, **overrides):
    data = {
        "general": {"device_ip": "127.0.0.1", "device_udp_port": device_port,
                    "device_ble_mac": "", "device_type": "",
                    "mqtt_host": BROKER_HOST, "mqtt_port": BROKER_PORT,
                    "mqtt_username": "", "mqtt_password": ""},
        "init": {"base_timeout_s": 0.2, "timeout_increment_s": 0.1, "max_retries": 2,
                 "inter_command_delay_s": 0.0},
        "message_settings": {"timeout_s": 0.3, "max_retry": 2, "min_inter_message_delay_s": 0},
        "status_polling": {"bat_status_interval_s": 999999, "es_mode_interval_s": 999999,
                            "es_status_interval_s": 0.3},
        "controller": {"deadzone_w": 5, "min_setpoint_change_w": 5, "max_step_w": 2000,
                        "min_output_w": -1500, "max_output_w": 800, "min_send_interval_s": 0},
        "passive_mode": {"power": 50, "cd_time": 60, "max_cd_time": 3600},
    }
    for k, v in overrides.items():
        data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.update({k: v})
    cfg = MarstekConfig.from_dict(data, path=tmp_path / "config.yaml")
    cfg.save()
    return MarstekConfig.load(tmp_path / "config.yaml")


async def _collect(topic_filter, count, timeout=3.0):
    messages = []
    async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier=f"ha-sim-{topic_filter.replace('/', '_')}") as c:
        await c.subscribe(topic_filter, qos=1)
        try:
            async with asyncio.timeout(timeout):
                async for msg in c.messages:
                    messages.append((str(msg.topic), msg.payload.decode()))
                    if len(messages) >= count:
                        break
        except TimeoutError:
            pass
    return messages


@pytest.mark.asyncio
async def test_bridge_full_startup_registers_entities_and_polls_status(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port)

    bridge = MarstekBridge(cfg)
    discovery_collector = asyncio.ensure_future(
        _collect("homeassistant/+/marstek_50cf14640fac/+/config", 5, timeout=5.0)
    )
    await asyncio.sleep(0.2)

    try:
        await bridge.start()
        discovery_msgs = await discovery_collector
        assert len(discovery_msgs) >= 5, "Es sollten mehrere Discovery-Configs veroeffentlicht werden"

        # ES.GetStatus wird alle 0.3s gepollt -> Wert sollte bald ankommen
        es_status_msgs = await _collect("Marstek-Bridge-Control/es_bat_soc/state", 1, timeout=2.0)
        assert es_status_msgs and es_status_msgs[0][1] == "91"

        # Init-Werte wurden veroeffentlicht
        model_msgs = await _collect("Marstek-Bridge-Control/device_model/state", 1, timeout=2.0)
        assert model_msgs and model_msgs[0][1] == "VenusC"

        # system_ready muss ON sein (retained), da die Init-Sequenz erfolgreich war
        ready_msgs = await _collect("Marstek-Bridge-Control/system_ready/state", 1, timeout=2.0)
        assert ready_msgs and ready_msgs[0][1] == "ON"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_system_ready_goes_false_on_graceful_stop(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port)

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        ready_collector = asyncio.ensure_future(
            _collect("Marstek-Bridge-Control/system_ready/state", 1, timeout=3.0)
        )
        await asyncio.sleep(0.2)
        await bridge.stop()
        msgs = await ready_collector
        assert msgs and msgs[-1][1] == "OFF"
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_communication_fail_published_when_device_stops_responding(tmp_path):
    """Simuliert genau das Szenario, das eine Push-Notification ausloesen soll:
    das Geraet haengt/antwortet nicht mehr, waehrend der Poll-Zyklus laeuft."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port)

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        # zunaechst muss communication_fail=OFF sein (Init war erfolgreich)
        initial = await _collect("Marstek-Bridge-Control/communication_fail/state", 1, timeout=2.0)
        assert initial and initial[0][1] == "OFF"

        fail_collector = asyncio.ensure_future(
            _collect("Marstek-Bridge-Control/communication_fail/state", 2, timeout=5.0)
        )
        await asyncio.sleep(0.1)  # retained "OFF" wird dem neuen Subscriber sofort erneut zugestellt

        # Geraet "haengt sich auf": ES.GetStatus antwortet ab jetzt nie mehr
        server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=999))

        msgs = await fail_collector
        assert msgs and msgs[-1][1] == "ON", "communication_fail sollte nach ausgeschoepften Retries ON werden"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_control_command_not_delayed_by_long_poll_interval(tmp_path):
    """Verifiziert, dass der Poll-Loop-Umbau (erst schlafen, dann pollen)
    Control-Kommandos NICHT verzoegert: auch bei einem absichtlich sehr
    langen Poll-Intervall muss ein Control-Kommando sofort verarbeitet
    werden, weil es unabhaengig ueber die Control-Queue laeuft."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    # ALLE Poll-Intervalle absichtlich riesig -> die Poll-Loops schlafen
    # praktisch "fuer immer" nach dem Start
    cfg = _test_config(tmp_path, port, status_polling={
        "bat_status_interval_s": 999999, "es_mode_interval_s": 999999, "es_status_interval_s": 999999
    })

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        t_start = time.monotonic()
        await bridge.start()

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-fastcontrol") as ha:
            await ha.publish("Marstek-Bridge-Control/energy_mode/set", "Passive", qos=1)

        await asyncio.sleep(0.3)
        elapsed = time.monotonic() - t_start

        set_mode_calls = [r for r in server.received if r["method"] == "ES.SetMode"]
        assert set_mode_calls, "ES.SetMode wurde nicht gesendet - Control-Kommando wurde blockiert!"
        assert elapsed < 2.0, f"Control-Kommando kam viel zu spaet an ({elapsed:.2f}s)"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_ha_select_passive_mode_sends_es_setmode(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port)

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-select") as ha:
            await ha.publish("Marstek-Bridge-Control/energy_mode/set", "Passive", qos=1)

        await asyncio.sleep(0.5)

        set_mode_calls = [r for r in server.received if r["method"] == "ES.SetMode"]
        assert set_mode_calls, "ES.SetMode wurde nicht aufgerufen"
        last_call = set_mode_calls[-1]
        assert last_call["params"]["config"]["mode"] == "Passive"
        assert last_call["params"]["config"]["passive_cfg"]["power"] == 50
        assert last_call["params"]["config"]["passive_cfg"]["cd_time"] == 60
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_shelly_power_drives_passive_controller_and_sends_command(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"},
                        passive_mode={"power": 1500, "cd_time": 60, "max_cd_time": 3600})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim") as shelly:
            # Haushalt bezieht 300W aus dem Netz (kein vorheriger Sollwert,
            # Basis=0W) -> integrale Regelung: neuer Sollwert = 0 + 300 = 300
            # (einspeisen/entladen, um den Netzbezug zu decken)
            await shelly.publish("shellies/em/power", "300", qos=0)

        await asyncio.sleep(0.5)

        set_mode_calls = [r for r in server.received if r["method"] == "ES.SetMode"]
        passive_calls = [c for c in set_mode_calls if c["params"]["config"]["mode"] == "Passive"]
        assert passive_calls, "Passive-Regler hat kein ES.SetMode gesendet"
        last_power = passive_calls[-1]["params"]["config"]["passive_cfg"]["power"]
        assert last_power == 300
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_ha_can_dynamically_lower_passive_power_cap_via_soc_automation(tmp_path):
    """Kernanforderung: eine HA-Automatisierung (z.B. SOC-abhaengig) soll
    passive_default_power live absenken koennen, und das muss den
    automatischen (Shelly-getriebenen) Regler SOFORT beeinflussen - nicht
    erst nach einem Neustart oder nur kosmetisch."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        # SOC ist niedrig -> Automatisierung senkt den Deckel auf 150W
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-soc-automation") as ha:
            await ha.publish("Marstek-Bridge-Control/passive_default_power/set", "150", qos=1)
        await asyncio.sleep(0.2)
        assert bridge._passive_power_cap == 150.0
        assert bridge.passive_ctrl._discharge_cap_w == 150.0

        # Haushalt bezieht 500W aus dem Netz (kein vorheriger Sollwert, Basis=0W)
        # -> integrale Regelung wuerde 0+500=500W einspeisen/entladen wollen,
        # der SOC-Deckel (150W) muss das begrenzen.
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-soc") as shelly:
            await shelly.publish("shellies/em/power", "500", qos=0)
        await asyncio.sleep(0.4)

        passive_calls = [r for r in server.received
                          if r["method"] == "ES.SetMode" and r["params"]["config"]["mode"] == "Passive"]
        assert passive_calls, "Passive-Regler hat kein ES.SetMode gesendet"
        assert passive_calls[-1]["params"]["config"]["passive_cfg"]["power"] == 150, (
            "Der SOC-Deckel (150W) haette die Ausgabe begrenzen muessen"
        )
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_manual_passive_select_uses_live_power_not_static_config(tmp_path):
    """Regressionstest fuer den urspruenglichen Bug: manuelles Umschalten auf
    'Passive' im Dropdown muss den zuletzt in HA gesetzten Wert verwenden,
    nicht den unveraenderlichen Startwert aus der Config."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port)

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-manual") as ha:
            await ha.publish("Marstek-Bridge-Control/passive_default_power/set", "222", qos=1)
            await asyncio.sleep(0.2)
            await ha.publish("Marstek-Bridge-Control/energy_mode/set", "Passive", qos=1)

        await asyncio.sleep(0.4)
        passive_calls = [r for r in server.received
                          if r["method"] == "ES.SetMode" and r["params"]["config"]["mode"] == "Passive"]
        assert passive_calls
        assert passive_calls[-1]["params"]["config"]["passive_cfg"]["power"] == 222
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_selecting_passive_via_ha_auto_enables_control_logic_debug(tmp_path):
    """End-to-End: manuelles Umschalten auf Passive ueber HA muss den
    ControlLogic-Logger automatisch auf DEBUG stellen, damit sichtbar wird,
    ob Shelly-Nachrichten ankommen - ohne die Config vorher anpassen zu
    muessen. Ausserdem muss danach tatsaechlich ein Shelly-Rohwert als
    DEBUG-Zeile auf diesem Logger auftauchen."""
    import logging as _logging

    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"})

    ctrl_logger = _logging.getLogger("marstek.control_logic")
    ctrl_logger.setLevel(_logging.WARNING)  # statisch aus

    captured_records = []
    capture_handler = _logging.Handler()
    capture_handler.emit = lambda record: captured_records.append(record)
    ctrl_logger.addHandler(capture_handler)

    bridge = MarstekBridge(cfg, debug_control_logic=False)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        assert ctrl_logger.level == _logging.WARNING

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-enable-debug") as ha:
            await ha.publish("Marstek-Bridge-Control/energy_mode/set", "Passive", qos=1)
        await asyncio.sleep(0.3)

        assert ctrl_logger.level == _logging.DEBUG, "Passive-Aktivierung haette ControlLogic-Debug einschalten muessen"

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-debugcheck") as shelly:
            await shelly.publish("shellies/em/power", "-250", qos=0)
        await asyncio.sleep(0.3)

        messages = [r.getMessage() for r in captured_records]
        assert any("Shelly-Eingang" in m for m in messages), (
            "Shelly-Rohwert haette als ControlLogic-Debug-Zeile auftauchen muessen"
        )
    finally:
        ctrl_logger.removeHandler(capture_handler)
        ctrl_logger.setLevel(_logging.NOTSET)
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_passive_resend_button_resends_command_and_resets_countdown(tmp_path):
    """Kernanforderung: der 'passive_resend'-Button muss das Passive-
    Kommando erneut senden UND den Countdown ('Passive Countdown
    Remaining') dabei zuruecksetzen - auch wenn der Modus bereits aktiv
    war (kein Moduswechsel noetig). Prueft den Countdown-Deadline direkt
    im Bridge-Objekt (praeziser als ueber die 1s-Tick-MQTT-Anzeige)."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, passive_mode={"power": 200, "cd_time": 60, "max_cd_time": 3600})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-initial-passive") as ha:
            await ha.publish("Marstek-Bridge-Control/energy_mode/set", "Passive", qos=1)
        await asyncio.sleep(0.5)

        deadline_after_first = bridge._passive_cd_deadline
        assert deadline_after_first is not None

        await asyncio.sleep(2.0)  # Zeit verstreichen lassen, Countdown laeuft

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-resend") as ha:
            await ha.publish("Marstek-Bridge-Control/passive_resend/set", "PRESS", qos=1)
        await asyncio.sleep(0.5)

        deadline_after_resend = bridge._passive_cd_deadline
        assert deadline_after_resend is not None
        assert deadline_after_resend > deadline_after_first, (
            "Der Resend-Button haette den Countdown-Deadline nach vorne verschieben (zuruecksetzen) muessen"
        )

        # Und: es muss tatsaechlich ein zweites ES.SetMode(Passive) beim Geraet angekommen sein
        passive_calls = [r for r in server.received
                          if r["method"] == "ES.SetMode" and r["params"]["config"]["mode"] == "Passive"]
        assert len(passive_calls) >= 2, "Der Button haette ein zweites ES.SetMode(Passive) senden muessen"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_integral_control_law_stabilizes_instead_of_runaway(tmp_path):
    """Regressionstest fuer den in der Praxis beobachteten Fehler: der
    Shelly misst den TATSAECHLICHEN Netzbezug inklusive der aktuellen
    Batterieleistung. Eine direkte 'target = -shelly' Abbildung wuerde bei
    steigendem Netzbezug (z.B. weil der Marstek selbst gerade laedt) immer
    staerker laden -> Mitkopplung/Aufschaukeln. Die korrekte integrale
    Regelung (neuer Sollwert = aktueller Sollwert + Netzbezug) muss
    stattdessen zur Stabilisierung tendieren, nicht zum Aufschaukeln."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"},
                        controller={"deadzone_w": 1, "min_setpoint_change_w": 1, "max_step_w": 5000,
                                    "min_output_w": -1500, "max_output_w": 800, "min_send_interval_s": 0},
                        passive_mode={"power": 1500, "cd_time": 60, "max_cd_time": 3600})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        # Simuliert exakt das beobachtete Szenario: Haushaltslast konstant bei
        # 310W (unabhaengig vom Batteriezustand). Erste Messung erfolgt WAEHREND
        # der Sollwert noch 0 ist -> Netzbezug = 310 - 0 = 310.
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-stability") as shelly:
            await shelly.publish("shellies/em/power", "310", qos=0)
        await asyncio.sleep(0.3)

        first_power = bridge.passive_ctrl.state.last_sent_setpoint_w
        # Bei Basis=0 sollte die Regelung ca. +310 (voll einspeisen/entladen) anstreben
        assert first_power == pytest.approx(310, abs=5)

        # Nach dieser Aktion "sieht" der Shelly (bei konstanter Last von 310W und
        # jetzigem Sollwert von ~310) einen Netzbezug nahe 0 - die Regelung
        # sollte sich stabilisieren, NICHT weiter eskalieren.
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-stability2") as shelly:
            await shelly.publish("shellies/em/power", "0", qos=0)
        await asyncio.sleep(0.3)

        second_power = bridge.passive_ctrl.state.last_sent_setpoint_w
        # Sollwert darf sich jetzt kaum noch aendern (stabiler Zustand erreicht),
        # NICHT immer weiter in dieselbe Richtung explodieren
        assert abs(second_power - first_power) < 20, (
            f"Regelung eskaliert statt zu stabilisieren: {first_power} -> {second_power}"
        )
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_es_mode_polling_can_be_disabled_but_init_still_calls_it(tmp_path):
    """Kernanforderung: es_mode_enabled=False darf den periodischen
    ES.GetMode-Poll abschalten, MUSS aber die Init-Sequenz unberuehrt
    lassen (dort wird ES.GetMode immer genau einmal abgefragt)."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, status_polling={
        "bat_status_interval_s": 999999, "es_mode_interval_s": 0.3,
        "es_status_interval_s": 999999, "es_mode_enabled": False,
    })

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        # Init-Sequenz muss ES.GetMode trotzdem genau einmal aufgerufen haben
        init_es_mode_calls = [r for r in server.received if r["method"] == "ES.GetMode"]
        assert len(init_es_mode_calls) == 1, "Init-Sequenz haette ES.GetMode genau 1x abfragen muessen"

        # Poll-Zyklus laenger laufen lassen als das (sehr kurze) Intervall waere -
        # es darf trotzdem kein weiterer ES.GetMode-Call dazukommen
        await asyncio.sleep(1.0)
        es_mode_calls_after = [r for r in server.received if r["method"] == "ES.GetMode"]
        assert len(es_mode_calls_after) == 1, (
            f"Periodisches ES.GetMode-Polling haette deaktiviert sein muessen, "
            f"aber es kamen {len(es_mode_calls_after)} Aufrufe an"
        )
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_es_mode_polling_enabled_by_default(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, status_polling={
        "bat_status_interval_s": 999999, "es_mode_interval_s": 0.3, "es_status_interval_s": 999999,
    })

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        await asyncio.sleep(0.8)
        es_mode_calls = [r for r in server.received if r["method"] == "ES.GetMode"]
        # 1x Init + mind. 1x periodischer Poll (0.3s Intervall, 0.8s gewartet)
        assert len(es_mode_calls) >= 2, "Standardmaessig sollte ES.GetMode periodisch gepollt werden"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_pv_disabled_by_default_no_init_call_no_poll(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("PV.GetStatus", MethodBehavior(
        result={"pv1_power": 100, "pv1_voltage": 30, "pv1_current": 3, "pv1_state": 1}))
    cfg = _test_config(tmp_path, port, status_polling={
        "bat_status_interval_s": 999999, "es_mode_interval_s": 999999,
        "es_status_interval_s": 999999, "pv_status_interval_s": 0.3,
    })  # pv_enabled Default = False

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        await asyncio.sleep(0.6)
        pv_calls = [r for r in server.received if r["method"] == "PV.GetStatus"]
        assert len(pv_calls) == 0, "PV.GetStatus haette bei deaktivierter Option nie aufgerufen werden duerfen"
        assert "pv1_power" not in bridge.bundle.entities
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_pv_enabled_polls_and_publishes_and_converts_state_to_bool(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("PV.GetStatus", MethodBehavior(
        result={"pv1_power": 150, "pv1_voltage": 32, "pv1_current": 4.5, "pv1_state": 1,
                "pv2_power": 0, "pv2_voltage": 0, "pv2_current": 0, "pv2_state": 0}))
    cfg = _test_config(tmp_path, port, status_polling={
        "bat_status_interval_s": 999999, "es_mode_interval_s": 999999,
        "es_status_interval_s": 999999, "pv_status_interval_s": 0.3, "pv_enabled": True,
    })

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        # Init-Sequenz muss PV.GetStatus 1x abgefragt haben
        init_pv_calls = [r for r in server.received if r["method"] == "PV.GetStatus"]
        assert len(init_pv_calls) == 1

        power_msgs = await _collect("Marstek-Bridge-Control/pv1_power/state", 1, timeout=2.0)
        assert power_msgs and power_msgs[0][1] == "150"
        active_msgs = await _collect("Marstek-Bridge-Control/pv1_active/state", 1, timeout=2.0)
        assert active_msgs and active_msgs[0][1] == "ON"
        inactive_msgs = await _collect("Marstek-Bridge-Control/pv2_active/state", 1, timeout=2.0)
        assert inactive_msgs and inactive_msgs[0][1] == "OFF"

        # periodisches Polling muss ebenfalls laufen
        await asyncio.sleep(0.6)
        later_pv_calls = [r for r in server.received if r["method"] == "PV.GetStatus"]
        assert len(later_pv_calls) >= 2, "PV.GetStatus haette auch periodisch gepollt werden sollen"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_pv_disabled_removes_previously_registered_entities(tmp_path):
    """Wird PV deaktiviert (Default), muessen eventuell zuvor registrierte
    PV-Discovery-Eintraege aktiv entfernt werden (leere retained Nachricht),
    damit sie nicht mehr in HA auftauchen."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port)  # pv_enabled Default = False

    bridge = MarstekBridge(cfg)
    collector = asyncio.ensure_future(
        _collect("homeassistant/sensor/marstek_50cf14640fac/pv1_power/config", 2, timeout=3.0)
    )
    await asyncio.sleep(0.2)  # evtl. retained Reste aus vorherigen Tests werden sofort zugestellt
    try:
        await bridge.start()
        msgs = await collector
        assert msgs[-1][1] == "", (
            "Bei deaktiviertem PV sollte eine leere Discovery-Nachricht fuer pv1_power gesendet werden"
        )
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_passive_controller_seeded_from_ongrid_power_after_restart(tmp_path):
    """Kernanforderung: nach dem Start muss der Passive-Regler NICHT von
    0W ausgehen, sondern vom tatsaechlichen aktuellen Geraetewert
    (ES.GetMode.ongrid_power aus der Init-Sequenz) - sonst wuerde die
    erste automatische Korrektur einen grossen, ungewollten Sprung
    verursachen."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("ES.GetMode", MethodBehavior(
        result={"mode": "Passive", "ongrid_power": 261, "offgrid_power": 0, "bat_soc": 33}))
    cfg = _test_config(tmp_path, port)

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        assert bridge.passive_ctrl.state.committed_setpoint_w == 261.0
        assert bridge.passive_ctrl.state.last_sent_setpoint_w == 261.0
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_first_shelly_update_after_restart_uses_seeded_setpoint_not_zero(tmp_path):
    """End-to-End-Beweis: die erste Shelly-getriebene Regelentscheidung nach
    dem Start rechnet mit dem geseedeten Sollwert (261W), nicht mit 0W."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("ES.GetMode", MethodBehavior(
        result={"mode": "Passive", "ongrid_power": 261, "offgrid_power": 0, "bat_soc": 33}))
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"},
                        controller={"deadzone_w": 1, "min_setpoint_change_w": 1, "max_step_w": 5000,
                                    "min_output_w": -1500, "max_output_w": 800, "min_send_interval_s": 0},
                        passive_mode={"power": 1500, "cd_time": 60, "max_cd_time": 3600})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        # kleine Abweichung vom Netzbezug -> Ziel = 261 + (-4.6) = 256.4 -> gerundet 256
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-seed") as shelly:
            await shelly.publish("shellies/em/power", "-4.6", qos=0)
        await asyncio.sleep(0.3)

        sent_power = bridge.passive_ctrl.state.last_sent_setpoint_w
        assert sent_power == pytest.approx(256, abs=1), (
            f"Erste Korrektur haette vom geseedeten 261W ausgehen sollen, nicht von 0W (war: {sent_power})"
        )
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_low_soc_pauses_automatic_regulation_no_send(tmp_path):
    """Kernanforderung: faellt der SOC unter die Idle-Schwelle, darf die
    Bridge KEINE Passive-Kommandos mehr senden (weder Update noch
    Keepalive) - die cd_time soll auf dem Geraet natuerlich ablaufen."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 3}))  # < Schwelle
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"},
                        controller={"deadzone_w": 1, "min_setpoint_change_w": 1, "max_step_w": 5000,
                                    "min_output_w": -1500, "max_output_w": 800, "min_send_interval_s": 0,
                                    "idle_soc_threshold": 5.0, "idle_soc_resume_margin": 3.0},
                        status_polling={"bat_status_interval_s": 999999, "es_mode_interval_s": 999999,
                                        "es_status_interval_s": 999999})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        assert bridge._latest_battery_soc == 3.0  # aus der Init-Sequenz uebernommen

        calls_before = len([r for r in server.received if r["method"] == "ES.SetMode"])
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-lowsoc") as shelly:
            await shelly.publish("shellies/em/power", "300", qos=0)
        await asyncio.sleep(0.3)

        calls_after = len([r for r in server.received if r["method"] == "ES.SetMode"])
        assert calls_after == calls_before, "Bei zu niedrigem SOC darf kein ES.SetMode gesendet werden"
        assert bridge._passive_idle_low_soc is True
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_soc_recovery_resumes_regulation_with_hysteresis(tmp_path):
    """Nach Erholung des SOC ueber threshold+margin muss die Regelung
    automatisch wieder anspringen."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 3}))
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"},
                        controller={"deadzone_w": 1, "min_setpoint_change_w": 1, "max_step_w": 5000,
                                    "min_output_w": -1500, "max_output_w": 800, "min_send_interval_s": 0,
                                    "idle_soc_threshold": 5.0, "idle_soc_resume_margin": 3.0},
                        status_polling={"bat_status_interval_s": 999999, "es_mode_interval_s": 999999,
                                        "es_status_interval_s": 999999},
                        passive_mode={"power": 1500, "cd_time": 60, "max_cd_time": 3600})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        # zunaechst niedriger SOC -> Idle
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-a") as shelly:
            await shelly.publish("shellies/em/power", "300", qos=0)
        await asyncio.sleep(0.2)
        assert bridge._passive_idle_low_soc is True

        # SOC erholt sich NUR knapp ueber die Schwelle (aber unter Hysterese) -> bleibt idle
        bridge._latest_battery_soc = 6.0  # < 5.0+3.0=8.0
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-b") as shelly:
            await shelly.publish("shellies/em/power", "300", qos=0)
        await asyncio.sleep(0.2)
        assert bridge._passive_idle_low_soc is True, "Hysterese haette ein zu frühes Wiederanspringen verhindern muessen"

        # SOC erholt sich ueber die Wiederaufnahme-Schwelle -> Regelung springt an
        bridge._latest_battery_soc = 9.0  # >= 8.0
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-c") as shelly:
            await shelly.publish("shellies/em/power", "300", qos=0)
        await asyncio.sleep(0.3)
        assert bridge._passive_idle_low_soc is False

        calls = [r for r in server.received if r["method"] == "ES.SetMode"
                 and r["params"]["config"]["mode"] == "Passive"]
        assert calls, "Nach Wiederaufnahme haette wieder gesendet werden muessen"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_soc_tracked_from_whichever_source_updates_last(tmp_path):
    """Kernanforderung: der SOC-Wert kommt vom jeweils zuletzt
    aktualisierenden Poll (Bat.GetStatus ODER ES.GetStatus)."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 50}))
    server.set_behavior("ES.GetStatus", MethodBehavior(result={"bat_soc": 42}))
    cfg = _test_config(tmp_path, port, status_polling={
        "bat_status_interval_s": 999999, "es_mode_interval_s": 999999, "es_status_interval_s": 0.3,
    })

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        # Init setzt zunaechst battery_status.soc (50) als Startwert
        assert bridge._latest_battery_soc == 50.0
        # ES.GetStatus pollt periodisch (0.3s) und aktualisiert bat_soc=42 danach
        await asyncio.sleep(0.5)
        assert bridge._latest_battery_soc == 42.0, "Der zuletzt aktualisierte Wert (ES.GetStatus) haette gewinnen muessen"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_idle_soc_threshold_live_adjustable_via_ha(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port)

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        assert bridge._idle_soc_threshold == 5.0  # Standard aus _test_config-Vererbung

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-idle-thresh") as ha:
            await ha.publish("Marstek-Bridge-Control/idle_soc_threshold/set", "10", qos=1)
        await asyncio.sleep(0.2)
        assert bridge._idle_soc_threshold == 10.0
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_restart_continuity_refreshes_cd_time_when_device_already_passive(tmp_path):
    """Kernanforderung: nach einem Neustart, waehrend das Geraet bereits im
    Passive-Mode laeuft, muss die Bridge nicht nur den internen Sollwert
    seeden, sondern auch aktiv den gleichen Wert erneut senden - damit die
    geraeteseitige cd_time frisch gesetzt und der lokale Countdown
    ('Passive Countdown Remaining') initialisiert wird."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("ES.GetMode", MethodBehavior(
        result={"mode": "Passive", "ongrid_power": 187, "offgrid_power": 0, "bat_soc": 33}))
    cfg = _test_config(tmp_path, port, passive_mode={"power": 800, "cd_time": 90, "max_cd_time": 3600})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        # Seeding korrekt uebernommen
        assert bridge.passive_ctrl.state.committed_setpoint_w == 187.0

        # UND: die cd_time wurde aktiv aufgefrischt (Countdown initialisiert)
        assert bridge._passive_cd_deadline is not None
        remaining = bridge._passive_cd_deadline - time.monotonic()
        assert remaining > 85, f"Countdown haette nahe der vollen cd_time (90s) starten sollen, war {remaining:.1f}s"

        # UND: es wurde tatsaechlich ein ES.SetMode(Passive, power=187) gesendet
        passive_calls = [r for r in server.received
                          if r["method"] == "ES.SetMode" and r["params"]["config"]["mode"] == "Passive"]
        assert passive_calls, "cd_time-Auffrischung haette ein ES.SetMode senden muessen"
        assert passive_calls[-1]["params"]["config"]["passive_cfg"]["power"] == 187
        assert passive_calls[-1]["params"]["config"]["passive_cfg"]["cd_time"] == 90
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_restart_continuity_does_not_force_passive_when_device_in_other_mode(tmp_path):
    """Regressionstest: ist das Geraet beim Neustart NICHT im Passive-Mode
    (z.B. Auto), darf die Bridge es NICHT ungefragt in Passive zwingen -
    nur der interne Sollwert wird geseedet, kein aktives Senden."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("ES.GetMode", MethodBehavior(
        result={"mode": "Auto", "ongrid_power": 150, "offgrid_power": 0, "bat_soc": 33}))
    cfg = _test_config(tmp_path, port)

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        assert bridge.passive_ctrl.state.committed_setpoint_w == 150.0
        assert bridge._passive_cd_deadline is None, "Countdown haette NICHT gestartet werden duerfen (Geraet ist im Auto-Mode)"

        passive_calls = [r for r in server.received
                          if r["method"] == "ES.SetMode" and r["params"]["config"]["mode"] == "Passive"]
        assert not passive_calls, "Es haette KEIN Passive-Kommando gesendet werden duerfen"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_shelly_send_failure_does_not_crash_message_loop(tmp_path):
    """Kernanforderung (in der Praxis beobachtet und gemeldet): schlaegt das
    Senden des Passive-Kommandos fehl (z.B. Timeout bei max_retry=0), darf
    das NICHT als unbehandelte Exception bis in die MQTT-Listen-Schleife
    durchschlagen - die Bridge muss weiterhin normal funktionieren."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("ES.SetMode", MethodBehavior(drop_first_n=999))  # Passive-Kommandos scheitern immer
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"},
                        message_settings={"timeout_s": 0.2, "max_retry": 0, "min_inter_message_delay_s": 0})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-fail") as shelly:
            await shelly.publish("shellies/em/power", "300", qos=0)
        await asyncio.sleep(0.5)  # Zeit fuer den fehlschlagenden Sendeversuch

        # Bridge muss danach weiterhin normal auf neue Nachrichten reagieren
        # (Beweis, dass die Listen-Schleife nicht abgestuerzt/haengengeblieben ist)
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-after-fail") as ha:
            await ha.publish("Marstek-Bridge-Control/passive_default_power/set", "222", qos=1)
        await asyncio.sleep(0.3)

        msgs = await _collect("Marstek-Bridge-Control/passive_default_power/state", 1, timeout=2.0)
        assert msgs and msgs[0][1] == "222.0", "Bridge haette nach dem fehlgeschlagenen Sendeversuch weiterlaufen muessen"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_countdown_keepalive_fires_without_any_shelly_messages(tmp_path):
    """Kernanforderung (in der Praxis beobachtet und gemeldet): das Keepalive
    darf NICHT ausschliesslich von Shelly-Nachrichten abhaengen - der
    unabhaengig laufende lokale Countdown muss selbst dann zuverlaessig
    ein Passive-Kommando erneut senden, wenn ueberhaupt keine Shelly-
    Nachricht eintrifft."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, passive_mode={"power": 196, "cd_time": 4, "max_cd_time": 3600})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-cdkeepalive") as ha:
            await ha.publish("Marstek-Bridge-Control/energy_mode/set", "Passive", qos=1)
        await asyncio.sleep(0.3)

        calls_after_activation = len([r for r in server.received if r["method"] == "ES.SetMode"])

        # Ohne JEDE Shelly-Nachricht: nach der Haelfte von cd_time (4s -> 2s)
        # muss der Countdown-Loop selbststaendig erneut senden.
        await asyncio.sleep(2.5)

        calls_after_wait = [r for r in server.received if r["method"] == "ES.SetMode"
                             and r["params"]["config"]["mode"] == "Passive"]
        assert len(calls_after_wait) > calls_after_activation, (
            "Der Countdown-Loop haette ohne jede Shelly-Nachricht selbststaendig "
            "erneut senden muessen"
        )
        assert calls_after_wait[-1]["params"]["config"]["passive_cfg"]["power"] == 196
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_countdown_keepalive_does_not_fire_while_soc_idle(tmp_path):
    """Waehrend SOC-Idle uebernimmt der eigene Idle-Keepalive-Task das
    Auffrischen (mit power~0) - der normale Countdown-Keepalive (mit dem
    zuletzt regulaer gesendeten Wert) darf sich damit nicht ueberschneiden."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 3}))  # < Idle-Schwelle
    cfg = _test_config(tmp_path, port, passive_mode={"power": 196, "cd_time": 4, "max_cd_time": 3600},
                        status_polling={"bat_status_interval_s": 999999, "es_mode_interval_s": 999999,
                                        "es_status_interval_s": 999999},
                        controller={"deadzone_w": 5, "min_setpoint_change_w": 5, "max_step_w": 2000,
                                    "min_output_w": -1500, "max_output_w": 800, "min_send_interval_s": 0,
                                    "idle_soc_threshold": 5.0, "idle_soc_resume_margin": 3.0})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-cdkeepalive2") as ha:
            await ha.publish("Marstek-Bridge-Control/energy_mode/set", "Passive", qos=1)
        await asyncio.sleep(0.3)
        # SOC-Idle wird beim naechsten Zyklus des Idle-Hintergrund-Tasks (max 5s) erkannt
        await asyncio.sleep(5.5)

        passive_calls = [r for r in server.received if r["method"] == "ES.SetMode"
                          and r["params"]["config"]["mode"] == "Passive"]
        # Alle gesendeten Werte muessen ~0 (Idle) sein, NICHT der reguläre
        # Sollwert 196 (das waere der Countdown-Keepalive, der hier nicht
        # zusaetzlich haette feuern duerfen)
        assert bridge._passive_idle_low_soc is True
        powers = [c["params"]["config"]["passive_cfg"]["power"] for c in passive_calls]
        # Vor der Idle-Erkennung (die separat alle 5s prueft) koennen bei
        # kurzer Test-cd_time durchaus mehrere legitime 196W-Keepalives
        # laufen - relevant ist nur, dass NACH der Idle-Erkennung kein
        # regulaerer 196W-Wert mehr gesendet wird.
        assert powers[-1] == 1, f"Nach Idle-Erkennung haette nur noch der Idle-Wert (~0W) gesendet werden duerfen: {powers}"
    finally:
        await bridge.stop()
        server.stop()
