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
    cfg = _test_config(tmp_path, port, shelly={"power_topic": "shellies/em/power"})

    bridge = MarstekBridge(cfg)
    await asyncio.sleep(0.1)
    try:
        await bridge.start()

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim") as shelly:
            # Haushalt bezieht 300W aus dem Netz -> Marstek soll mit -300W (laden) regeln? Ziel: -raw_power
            await shelly.publish("shellies/em/power", "300", qos=0)

        await asyncio.sleep(0.5)

        set_mode_calls = [r for r in server.received if r["method"] == "ES.SetMode"]
        passive_calls = [c for c in set_mode_calls if c["params"]["config"]["mode"] == "Passive"]
        assert passive_calls, "Passive-Regler hat kein ES.SetMode gesendet"
        last_power = passive_calls[-1]["params"]["config"]["passive_cfg"]["power"]
        assert last_power == -300
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

        # Haushalt braucht 500W aus dem Netz -> ohne Deckel wuerde die
        # Regelung mit -500W (aus Marstek-Sicht: einspeisen/entladen) senden,
        # der Deckel muss das auf 150W begrenzen.
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="shelly-sim-soc") as shelly:
            await shelly.publish("shellies/em/power", "-500", qos=0)
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
        # Mittelwert aus (-100,-100,-2000)/3 = -733 (Ziel=+733), NICHT der volle
        # Ausreisser-Zielwert von +2000
        assert last_power != 2000
        assert 600 <= last_power <= 800, f"unerwarteter gemittelter Wert: {last_power}"
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
