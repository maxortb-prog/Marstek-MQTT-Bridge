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
async def test_shelly_debounce_smooths_noisy_readings(tmp_path):
    """Kernanforderung: mehrere kurz aufeinanderfolgende Shelly-Werte werden
    ueber das konfigurierte Entprell-Fenster gemittelt, bevor sie in die
    Regellogik einfliessen - ein einzelner Ausreisser darf nicht direkt
    durchschlagen."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, shelly={
        "power_topic": "shellies/em/power", "debounce_time_s": 30,
    }, passive_mode={"power": 1500, "cd_time": 60, "max_cd_time": 3600})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        assert bridge.shelly_averager.window_s == 30

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-debounce") as shelly:
            await shelly.publish("shellies/em/power", "-100", qos=0)
            await asyncio.sleep(0.05)
            await shelly.publish("shellies/em/power", "-100", qos=0)
            await asyncio.sleep(0.05)
            # kurzer Ausreisser
            await shelly.publish("shellies/em/power", "-2000", qos=0)
        await asyncio.sleep(0.4)

        passive_calls = [r for r in server.received
                          if r["method"] == "ES.SetMode" and r["params"]["config"]["mode"] == "Passive"]
        assert passive_calls, "Passive-Regler haette senden muessen"
        last_power = passive_calls[-1]["params"]["config"]["passive_cfg"]["power"]
        # Ohne Entprellung wuerde die 3. Nachricht (Rohwert -2000, integral
        # auf den Sollwert von -200 aufaddiert) auf -2200 -> geklemmt auf
        # -1500 (min_output_w) fuehren. Mit 30s-Fenster wird stattdessen der
        # gemittelte Wert (-100,-100,-2000)/3=-733.3 verwendet: -200 + (-733.3)
        # = -933.3 -> deutlich weniger extrem als die geklemmte Grenze.
        assert last_power != -1500, "Entprellung haette den Ausreisser abfedern sollen (nicht bis ans Limit)"
        assert -960 <= last_power <= -900, f"unerwarteter gemittelter Wert: {last_power}"
    finally:
        await bridge.stop()
        server.stop()


@pytest.mark.asyncio
async def test_shelly_debounce_time_live_adjustable_via_ha(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_full_server_behavior(server)
    cfg = _test_config(tmp_path, port, shelly={
        "power_topic": "shellies/em/power", "debounce_time_s": 30,
    })

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()
        assert bridge.shelly_averager.window_s == 30

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-debounce") as ha:
            await ha.publish("Marstek-Bridge-Control/shelly_debounce_time_s/set", "5", qos=1)
        await asyncio.sleep(0.2)

        assert bridge.shelly_averager.window_s == 5
        assert bridge._shelly_debounce_time_s == 5.0
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
        assert any("Shelly-Eingang (roh)" in m for m in messages), (
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
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power", "debounce_time_s": 0},
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
