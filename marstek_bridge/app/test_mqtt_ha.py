"""
test_mqtt_ha.py

Testet mqtt_ha.py gegen einen lokal laufenden Mosquitto-Broker
(127.0.0.1:1883, kein Auth). Ausfuehren mit: pytest -v -s test_mqtt_ha.py
"""

import asyncio
import json
import logging

import aiomqtt
import pytest

from mqtt_ha import HAMqttBridge, HADevice, HAEntity

logging.basicConfig(level=logging.DEBUG)

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883


async def _collect_messages(topic_filter: str, count: int, timeout: float = 3.0):
    """Hilfsfunktion: simuliert Home Assistant - abonniert und sammelt Nachrichten."""
    messages = []
    async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="test-subscriber") as client:
        await client.subscribe(topic_filter, qos=1)
        try:
            async with asyncio.timeout(timeout):
                async for msg in client.messages:
                    messages.append((str(msg.topic), msg.payload.decode()))
                    if len(messages) >= count:
                        break
        except TimeoutError:
            pass
    return messages


@pytest.mark.asyncio
async def test_discovery_payload_for_sensor():
    hub = HADevice(identifiers=("marstek_hub",), name="Marstek System", suggested_area="Marstek")
    entity = HAEntity(
        component="sensor", object_id="battery_soc", name="Battery SOC",
        device=hub, unit_of_measurement="%", state_class="measurement",
    )

    bridge = HAMqttBridge(BROKER_HOST, BROKER_PORT, node_id="marstek_test1", base_topic="Marstek-Test1")
    collector = asyncio.ensure_future(
        _collect_messages("homeassistant/sensor/marstek_test1/battery_soc/config", 1)
    )
    await asyncio.sleep(0.2)  # sicherstellen, dass der Collector schon abonniert hat

    await bridge.connect()
    try:
        await bridge.register_entity(entity, initial_state=88)
        messages = await collector
        assert len(messages) == 1
        topic, payload = messages[0]
        data = json.loads(payload)
        assert data["unique_id"] == "marstek_test1_battery_soc"
        assert data["unit_of_measurement"] == "%"
        assert data["device"]["name"] == "Marstek System"
        assert data["state_topic"] == "Marstek-Test1/battery_soc/state"
        assert data["availability_topic"] == "Marstek-Test1/bridge/status"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_availability_online_offline_retained():
    bridge = HAMqttBridge(BROKER_HOST, BROKER_PORT, node_id="marstek_test2", base_topic="Marstek-Test2")

    collector = asyncio.ensure_future(_collect_messages("Marstek-Test2/bridge/status", 1))
    await asyncio.sleep(0.2)
    await bridge.connect()
    msgs = await collector
    assert msgs[-1][1] == "online"

    collector2 = asyncio.ensure_future(_collect_messages("Marstek-Test2/bridge/status", 2))
    await asyncio.sleep(0.2)  # retained "online" wird dem neuen Subscriber sofort zugestellt
    await bridge.close()
    msgs2 = await collector2
    assert msgs2[-1][1] == "offline"


@pytest.mark.asyncio
async def test_number_command_roundtrip():
    """Simuliert: HA aendert eine 'number'-Entity (z.B. DOD-Wert) -> Callback wird aufgerufen."""
    hub = HADevice(identifiers=("marstek_hub",), name="Marstek System")
    dod_entity = HAEntity(
        component="number", object_id="dod_value", name="DOD",
        device=hub, min_value=30, max_value=88, step=1,
    )

    received = []

    def on_command(entity, payload):
        received.append((entity.object_id, payload))

    bridge = HAMqttBridge(BROKER_HOST, BROKER_PORT, node_id="marstek_test3", base_topic="Marstek-Test3")
    bridge.set_command_callback(on_command)
    await bridge.connect()
    try:
        await bridge.register_entity(dod_entity)
        await asyncio.sleep(0.2)  # subscribe muss beim Broker angekommen sein

        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-simulator") as ha_client:
            await ha_client.publish("Marstek-Test3/dod_value/set", "45", qos=1)

        await asyncio.sleep(0.3)
        assert received == [("dod_value", "45")]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_select_entity_options_and_state_publish():
    hub = HADevice(identifiers=("marstek_hub",), name="Marstek Energy Control")
    mode_entity = HAEntity(
        component="select", object_id="energy_mode", name="Energy Mode",
        device=hub, options=["Auto", "AI", "Manual", "Passive", "Ups"],
    )

    bridge = HAMqttBridge(BROKER_HOST, BROKER_PORT, node_id="marstek_test4", base_topic="Marstek-Test4")

    collector = asyncio.ensure_future(
        _collect_messages("homeassistant/select/marstek_test4/energy_mode/config", 1)
    )
    await asyncio.sleep(0.2)
    await bridge.connect()
    try:
        await bridge.register_entity(mode_entity)
        msgs = await collector
        data = json.loads(msgs[0][1])
        assert data["options"] == ["Auto", "AI", "Manual", "Passive", "Ups"]
        assert data["command_topic"] == "Marstek-Test4/energy_mode/set"

        state_collector = asyncio.ensure_future(_collect_messages("Marstek-Test4/energy_mode/state", 1))
        await asyncio.sleep(0.2)
        await bridge.publish_state(mode_entity, "Passive")
        state_msgs = await state_collector
        assert state_msgs[0][1] == "Passive"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_button_entity_no_state_topic_and_press_routes_to_callback():
    """Button-Entities brauchen kein state_topic im Discovery-Payload (nicht
    Teil des HA-Schemas) und muessen bei jedem Press-Kommando den Callback
    aufrufen - unabhaengig von irgendeinem vorherigen Zustand."""
    hub = HADevice(identifiers=("marstek_hub",), name="Marstek Energy Control")
    resend_entity = HAEntity(
        component="button", object_id="passive_resend", name="Resend Passive Command",
        device=hub,
    )

    received = []

    def on_command(entity, payload):
        received.append((entity.object_id, payload))

    bridge = HAMqttBridge(BROKER_HOST, BROKER_PORT, node_id="marstek_test5", base_topic="Marstek-Test5")
    bridge.set_command_callback(on_command)

    collector = asyncio.ensure_future(
        _collect_messages("homeassistant/button/marstek_test5/passive_resend/config", 1)
    )
    await asyncio.sleep(0.2)
    await bridge.connect()
    try:
        await bridge.register_entity(resend_entity)
        msgs = await collector
        data = json.loads(msgs[0][1])
        assert "state_topic" not in data, "button darf keinen state_topic im Discovery-Payload haben"
        assert data["command_topic"] == "Marstek-Test5/passive_resend/set"
        assert data["payload_press"] == "PRESS"

        await asyncio.sleep(0.2)  # subscribe muss beim Broker angekommen sein
        async with aiomqtt.Client(BROKER_HOST, BROKER_PORT, identifier="ha-sim-button") as ha:
            await ha.publish("Marstek-Test5/passive_resend/set", "PRESS", qos=1)
            await asyncio.sleep(0.1)
            # zweiter Press mit demselben Payload -> muss trotzdem erneut ankommen
            await ha.publish("Marstek-Test5/passive_resend/set", "PRESS", qos=1)

        await asyncio.sleep(0.3)
        assert received == [("passive_resend", "PRESS"), ("passive_resend", "PRESS")], (
            "Jeder Press muss den Callback auslösen, auch bei identischem Payload"
        )
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_remove_entity_discovery_publishes_empty_retained_payload():
    hub = HADevice(identifiers=("marstek_hub",), name="Marstek PV")
    entity = HAEntity(component="sensor", object_id="pv1_power", name="PV1 Power", device=hub)

    bridge = HAMqttBridge(BROKER_HOST, BROKER_PORT, node_id="marstek_test6", base_topic="Marstek-Test6")
    await bridge.connect()
    try:
        await bridge.register_entity(entity)
        # sicherstellen, dass es vorher wirklich registriert war (retained config vorhanden)
        pre_msgs = await _collect_messages("homeassistant/sensor/marstek_test6/pv1_power/config", 1, timeout=2.0)
        assert pre_msgs

        collector = asyncio.ensure_future(
            _collect_messages("homeassistant/sensor/marstek_test6/pv1_power/config", 2, timeout=2.0)
        )
        await asyncio.sleep(0.2)  # retained alte Config wird dem neuen Subscriber sofort erneut zugestellt
        await bridge.remove_entity_discovery("sensor", "pv1_power")
        msgs = await collector
        assert msgs[-1][1] == "", "Entfernen muss eine leere Nachricht auf den Discovery-Topic senden"
    finally:
        await bridge.close()
