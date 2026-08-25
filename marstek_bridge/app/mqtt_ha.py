"""
mqtt_ha.py

Generische, wiederverwendbare Schicht fuer:
    - Verbindung zu einem MQTT-Broker (asyncio, via aiomqtt)
    - Home Assistant MQTT Discovery (sensor, binary_sensor, switch, select, number)
    - Availability-Handling ueber Last-Will + expliziten online/offline State
    - Command-Routing: eingehende Nachrichten auf Command-Topics (von HA
      gesendete Schalt-/Sollwert-Aenderungen) werden an einen einzigen,
      registrierten Callback weitergereicht.

Kennt bewusst KEINE Marstek-spezifische Logik (Feldnamen, Modi, DOD-Werte,
etc.) - das ist Aufgabe einer separaten Schicht, die HAEntity-Objekte fuer
die einzelnen API-Felder erzeugt und ueber diese Bridge registriert.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

import aiomqtt

logger = logging.getLogger("marstek.mqtt_ha")


# --------------------------------------------------------------------------- #
# HA-Geraete & Entities
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class HADevice:
    """Eine HA-'Geraete-Gruppe' - fasst mehrere Entities in der UI zusammen.

    Beispiel-Gruppen laut Spezifikation: 'Marstek System', 'Marstek Battery',
    'Marstek Energy Status', 'Marstek Energy Mode', 'Marstek Energy Control'.
    Alle koennen ueber via_device auf ein gemeinsames Hub-Geraet verweisen.
    """
    identifiers: tuple  # tuple statt list, damit HADevice hashable bleibt
    name: str
    manufacturer: str = "Marstek"
    model: Optional[str] = None
    suggested_area: Optional[str] = None
    via_device: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {
            "identifiers": list(self.identifiers),
            "name": self.name,
            "manufacturer": self.manufacturer,
        }
        if self.model:
            d["model"] = self.model
        if self.suggested_area:
            d["suggested_area"] = self.suggested_area
        if self.via_device:
            d["via_device"] = self.via_device
        return d


@dataclass
class HAEntity:
    """Basis fuer alle HA-Discovery-Entities.

    component: "sensor" | "binary_sensor" | "switch" | "select" | "number"
    object_id: eindeutig innerhalb der Bridge (z.B. 'battery_soc', 'led_ctrl')
    """
    component: str
    object_id: str
    name: str
    device: HADevice
    device_class: Optional[str] = None
    unit_of_measurement: Optional[str] = None
    state_class: Optional[str] = None
    icon: Optional[str] = None
    entity_category: Optional[str] = None  # "diagnostic" | "config"
    payload_on: str = "ON"                  # fuer switch/binary_sensor
    payload_off: str = "OFF"
    options: Optional[list] = None          # fuer "select"
    min_value: Optional[float] = None       # fuer "number"
    max_value: Optional[float] = None
    step: Optional[float] = None
    extra_config: dict = field(default_factory=dict)  # Fallback fuer Sonderfaelle

    # werden bei register_entity() von der Bridge gesetzt:
    state_topic: str = field(init=False, default="")
    command_topic: Optional[str] = field(init=False, default=None)
    unique_id: str = field(init=False, default="")

    @property
    def has_command_topic(self) -> bool:
        return self.component in ("switch", "select", "number")


CommandCallback = Callable[[HAEntity, str], Union[None, Awaitable[None]]]


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #

class HAMqttBridge:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        discovery_prefix: str = "homeassistant",
        base_topic: str = "Marstek-Bridge-Control",
        node_id: str = "marstek",
        client_id: str = "marstek-bridge",
        reconnect_interval_s: float = 5.0,
    ):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._discovery_prefix = discovery_prefix.rstrip("/")
        self._base_topic = base_topic.rstrip("/")
        self._node_id = node_id
        self._client_id = client_id
        self._reconnect_interval_s = reconnect_interval_s

        self.availability_topic = f"{self._base_topic}/bridge/status"

        self._client: Optional[aiomqtt.Client] = None
        self._entities: dict[str, HAEntity] = {}
        self._command_topic_map: dict[str, str] = {}
        self._raw_topic_callbacks: dict[str, Callable[[str, str], Union[None, Awaitable[None]]]] = {}
        self._callback: Optional[CommandCallback] = None
        self._listen_task: Optional[asyncio.Task] = None
        self.connected = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        will = aiomqtt.Will(topic=self.availability_topic, payload="offline", qos=1, retain=True)
        self._client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            identifier=self._client_id,
            will=will,
        )
        await self._client.__aenter__()
        self.connected = True
        await self._publish_availability(True)
        self._listen_task = asyncio.create_task(self._listen_loop())
        logger.info("MQTT verbunden mit %s:%s (client_id=%s)", self._host, self._port, self._client_id)

    async def close(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client and self.connected:
            try:
                await self._publish_availability(False)
            except Exception:
                logger.debug("Konnte 'offline' beim geordneten Trennen nicht mehr senden", exc_info=True)
            await self._client.__aexit__(None, None, None)
        self.connected = False

    async def _publish_availability(self, online: bool) -> None:
        await self._client.publish(
            self.availability_topic, "online" if online else "offline", qos=1, retain=True
        )

    # ------------------------------------------------------------------ #
    # Discovery / Registrierung
    # ------------------------------------------------------------------ #

    def set_command_callback(self, callback: CommandCallback) -> None:
        self._callback = callback

    async def register_entity(self, entity: HAEntity, *, initial_state: Any = None) -> None:
        unique_id = f"{self._node_id}_{entity.object_id}"
        entity.unique_id = unique_id
        entity.state_topic = f"{self._base_topic}/{entity.object_id}/state"
        if entity.has_command_topic:
            entity.command_topic = f"{self._base_topic}/{entity.object_id}/set"

        self._entities[unique_id] = entity
        if entity.command_topic:
            self._command_topic_map[entity.command_topic] = unique_id

        discovery_topic = (
            f"{self._discovery_prefix}/{entity.component}/{self._node_id}/{entity.object_id}/config"
        )
        payload = self._build_discovery_payload(entity)
        await self._client.publish(discovery_topic, json.dumps(payload), qos=1, retain=True)
        logger.debug("Discovery veroeffentlicht: %s -> %s", discovery_topic, payload)

        if entity.command_topic:
            await self._client.subscribe(entity.command_topic, qos=1)
            logger.debug("Abonniert: %s", entity.command_topic)

        if initial_state is not None:
            await self.publish_state(entity, initial_state)

    async def subscribe_raw(
        self, topic: str, callback: Callable[[str, str], Union[None, Awaitable[None]]]
    ) -> None:
        """Abonniert ein beliebiges, nicht zu einer Entity gehoerendes Topic
        (z.B. eine externe Shelly-Leistungsmessung) und routet eingehende
        Nachrichten an 'callback(topic, payload)'. Kein Wildcard-Matching -
        exakter Topic-String."""
        self._raw_topic_callbacks[topic] = callback
        await self._client.subscribe(topic, qos=0)
        logger.debug("Raw-Topic abonniert: %s", topic)

    def _build_discovery_payload(self, entity: HAEntity) -> dict:
        payload: dict = {
            "name": entity.name,
            "unique_id": entity.unique_id,
            "state_topic": entity.state_topic,
            "device": entity.device.to_dict(),
            "availability_topic": self.availability_topic,
        }
        if entity.device_class:
            payload["device_class"] = entity.device_class
        if entity.unit_of_measurement:
            payload["unit_of_measurement"] = entity.unit_of_measurement
        if entity.state_class:
            payload["state_class"] = entity.state_class
        if entity.icon:
            payload["icon"] = entity.icon
        if entity.entity_category:
            payload["entity_category"] = entity.entity_category
        if entity.command_topic:
            payload["command_topic"] = entity.command_topic
        if entity.component in ("switch",):
            payload["payload_on"] = entity.payload_on
            payload["payload_off"] = entity.payload_off
        if entity.component == "binary_sensor":
            payload["payload_on"] = entity.payload_on
            payload["payload_off"] = entity.payload_off
        if entity.component == "select" and entity.options:
            payload["options"] = entity.options
        if entity.component == "number":
            if entity.min_value is not None:
                payload["min"] = entity.min_value
            if entity.max_value is not None:
                payload["max"] = entity.max_value
            if entity.step is not None:
                payload["step"] = entity.step
        payload.update(entity.extra_config)
        return payload

    # ------------------------------------------------------------------ #
    # State-Publishing (Bridge -> HA)
    # ------------------------------------------------------------------ #

    async def publish_state(self, entity: HAEntity, value: Any, *, retain: bool = True) -> None:
        if isinstance(value, str):
            payload = value
        elif isinstance(value, bool):
            payload = entity.payload_on if value else entity.payload_off
        elif isinstance(value, (dict, list)):
            payload = json.dumps(value)
        else:
            payload = str(value)
        await self._client.publish(entity.state_topic, payload, qos=0, retain=retain)

    # ------------------------------------------------------------------ #
    # Command-Handling (HA -> Bridge)
    # ------------------------------------------------------------------ #

    async def _listen_loop(self) -> None:
        try:
            async for message in self._client.messages:
                topic = str(message.topic)
                payload = (
                    message.payload.decode()
                    if isinstance(message.payload, (bytes, bytearray))
                    else str(message.payload)
                )

                raw_cb = self._raw_topic_callbacks.get(topic)
                if raw_cb is not None:
                    logger.debug("RAW-Nachricht empfangen: topic=%s payload=%s", topic, payload)
                    result = raw_cb(topic, payload)
                    if asyncio.iscoroutine(result):
                        await result
                    continue

                unique_id = self._command_topic_map.get(topic)
                if unique_id is None:
                    continue
                entity = self._entities.get(unique_id)
                logger.debug("COMMAND empfangen: topic=%s payload=%s", topic, payload)
                if self._callback is not None and entity is not None:
                    result = self._callback(entity, payload)
                    if asyncio.iscoroutine(result):
                        await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fehler in der MQTT-Listen-Schleife")
