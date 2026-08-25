"""
fake_marstek_server.py

Simuliert ein Marstek-Geraet auf UDP fuer lokale Tests des udp_client.py,
OHNE echte Hardware. Erlaubt gezieltes Simulieren von:
    - normaler Antwort (Echo der id + Dummy-Result)
    - Verzoegerung (delay_s)
    - "Drop" (keine Antwort -> erzwingt Timeout/Retry beim Client)
    - Geraeteseitigem Fehler (JSON-RPC 'error')
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("marstek.fake_server")


@dataclass
class MethodBehavior:
    delay_s: float = 0.0
    drop_first_n: int = 0          # die ersten N Aufrufe dieser Methode ignorieren (kein Reply)
    error: Optional[dict] = None   # falls gesetzt: JSON-RPC error statt result
    result: dict = field(default_factory=dict)
    _call_count: int = 0


class FakeMarstekProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: "FakeMarstekServer"):
        self._server = server

    def connection_made(self, transport):
        self._server._transport = transport

    def datagram_received(self, data: bytes, addr):
        asyncio.get_event_loop().create_task(self._server._handle(data, addr))


class FakeMarstekServer:
    def __init__(self, host="127.0.0.1", port=0):
        self._host = host
        self._port = port
        self._transport: Optional[asyncio.DatagramTransport] = None
        self.behaviors: dict[str, MethodBehavior] = {}
        self.received: list[dict] = []  # Log aller eingegangenen Requests, fuer Test-Assertions

    def set_behavior(self, method: str, behavior: MethodBehavior) -> None:
        self.behaviors[method] = behavior

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: FakeMarstekProtocol(self),
            local_addr=(self._host, self._port),
        )
        self._transport = transport
        sock = transport.get_extra_info("socket")
        actual_port = sock.getsockname()[1]
        self._port = actual_port
        logger.info("Fake-Marstek-Server laeuft auf %s:%s", self._host, actual_port)
        return actual_port

    def stop(self) -> None:
        if self._transport:
            self._transport.close()

    async def _handle(self, data: bytes, addr) -> None:
        try:
            req = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return
        method = req.get("method")
        req_id = req.get("id")
        self.received.append({"method": method, "params": req.get("params")})
        behavior = self.behaviors.get(method, MethodBehavior())
        behavior._call_count += 1

        if behavior._call_count <= behavior.drop_first_n:
            logger.debug("Server: DROP #%d fuer method=%s id=%s", behavior._call_count, method, req_id)
            return  # keine Antwort simulieren

        if behavior.delay_s:
            await asyncio.sleep(behavior.delay_s)

        if behavior.error:
            response = {"id": req_id, "src": "FakeDevice", "error": behavior.error}
        else:
            response = {"id": req_id, "src": "FakeDevice", "result": {"id": 0, **behavior.result}}

        self._transport.sendto(json.dumps(response).encode("utf-8"), addr)
