"""
udp_client.py

Asyncio-UDP-Client fuer die Kommunikation mit einem Marstek-Geraet
(JSON-RPC-artiges Protokoll, siehe "Marstek Device Open API").

Kernanforderungen aus der Spezifikation:
    - Control-Kommandos (ES.SetMode, DOD.SET, Ble.Adv, Led.Ctrl) haben
      ABSOLUTE Prioritaet gegenueber Status-Abfragen (Bat.GetStatus,
      ES.GetMode, ES.GetStatus, ...).
    - Das Geraet kann physisch immer nur EINEN Request gleichzeitig
      verarbeiten. Ein laufender Wartevorgang auf eine Status-Antwort wird
      daher NICHT mitten im Warten unterbrochen (es wuerde ja ohnehin keine
      zweite Antwort gleichzeitig zurueckkommen). Stattdessen gilt:
      Endet eine Status-Abfrage in einem Timeout, werden - BEVOR der
      naechste Retry-Versuch gestartet wird - alle wartenden Control-
      Kommandos zuerst vollstaendig abgearbeitet (inkl. deren eigener
      Retry-Logik, im schlechtesten Fall bis zum eigenen Timeout/Fehler).
      Erst danach wird der naechste Retry-Versuch der Status-Abfrage
      gestartet - im guenstigen Fall antwortet das Geraet dann wieder normal.
    - Zwei getrennte Retry-Profile:
        InitRetryPolicy   -> Erstinitialisierung: Basis-Timeout 2s, pro
                              Versuch +5s, bis zu 5 Versuche insgesamt.
        RuntimeRetryPolicy-> laufender Betrieb: fixer Timeout 1s,
                              max_retries 0-10 (konfigurierbar).
    - Wird bei einem Kommando im laufenden Betrieb die maximale Anzahl an
      Versuchen erreicht -> Communication-Fail-Zustand setzen,
      Communication-Established zuruecksetzen, Watchdog-Callback ausloesen.
    - Farblich unterscheidbares Debug-Logging fuer Status- vs.
      Control-Kommandos vs. Init- vs. Verbindungsstatus-Meldungen.

Dieses Modul kennt keine MQTT-/Home-Assistant-Logik. Es stellt nur die
Transportschicht bereit: send_status(), send_control(), send_init().
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("marstek.udp_client")


# --------------------------------------------------------------------------- #
# Farbiges Logging (Status vs. Control vs. Init vs. Verbindungsstatus)
# --------------------------------------------------------------------------- #

class _Category(str, Enum):
    STATUS = "STATUS"
    CONTROL = "CONTROL"
    INIT = "INIT"
    COMM = "COMM"
    CONTROLLOGIC = "CONTROLLOGIC"  # Regler-internes Debugging (bridge.py/
                                   # passive_controller.py), eigener Logger
                                   # "marstek.control_logic", separat vom
                                   # allgemeinen log_level schaltbar.


class ColorCategoryFormatter(logging.Formatter):
    """Faerbt Logzeilen je nach 'category' (extra={'category': ...}) ein.
    Andere Module koennen statt der Enum-Member auch einfache Strings
    uebergeben (z.B. extra={'category': 'CONTROLLOGIC'}) - da _Category von
    str erbt, sind Hash/Gleichheit identisch und die Dict-Lookup funktioniert
    ohne dass diese Module von udp_client.py abhaengig sein muessen."""

    _COLORS = {
        _Category.STATUS: "\033[36m",       # cyan (Standard-SGR statt "bright"-Variante
        _Category.CONTROL: "\033[33m",      # gelb  fuer bessere Kompatibilitaet mit
        _Category.INIT: "\033[35m",         # magenta ANSI->HTML-Konvertern, z.B. im
        _Category.COMM: "\033[31m",         # rot   HA-Add-on-Log-Viewer)
        _Category.CONTROLLOGIC: "\033[32m", # gruen (Regler-Debugging)
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        category = getattr(record, "category", None)
        color = self._COLORS.get(category)
        return f"{color}{base}{self._RESET}" if color else base


def setup_default_logging(level=logging.DEBUG) -> None:
    """Bequeme Default-Einrichtung fuer Standalone-Betrieb/Tests."""
    handler = logging.StreamHandler()
    handler.setFormatter(ColorCategoryFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


# --------------------------------------------------------------------------- #
# Fehler & Retry-Policies
# --------------------------------------------------------------------------- #

class MarstekDeviceError(Exception):
    """Vom Geraet als JSON-RPC 'error' zurueckgemeldeter Fehler."""

    def __init__(self, error_obj: dict):
        self.code = error_obj.get("code")
        self.message = error_obj.get("message")
        self.data = error_obj.get("data")
        super().__init__(f"Marstek-Fehler {self.code}: {self.message} (data={self.data})")


class MarstekCommunicationError(Exception):
    """Alle Versuche (inkl. Retries) sind ohne gueltige Antwort fehlgeschlagen."""


@dataclass
class InitRetryPolicy:
    """Wird nur waehrend der Erstinitialisierung verwendet."""

    base_timeout_s: float = 2.0
    timeout_increment_s: float = 5.0
    max_retries: int = 4  # + 1 Erstversuch = bis zu 5 Abfragen insgesamt

    def timeout_for_attempt(self, attempt: int) -> float:
        # attempt beginnt bei 1
        return self.base_timeout_s + self.timeout_increment_s * (attempt - 1)

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1


@dataclass
class RuntimeRetryPolicy:
    """Wird im laufenden Poll-/Control-Betrieb verwendet ("Message Settings")."""

    timeout_s: float = 1.0
    max_retries: int = 3  # 0-10, konfigurierbar

    def __post_init__(self):
        if not (0 <= self.max_retries <= 10):
            raise ValueError("max_retries muss zwischen 0 und 10 liegen")

    def timeout_for_attempt(self, attempt: int) -> float:
        return self.timeout_s

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1


# --------------------------------------------------------------------------- #
# Queue-Item
# --------------------------------------------------------------------------- #

class CommandCategory(str, Enum):
    STATUS = "STATUS"
    CONTROL = "CONTROL"


@dataclass
class _QueueItem:
    method: str
    params: dict
    category: CommandCategory
    future: "asyncio.Future"
    retry_policy: Optional[Any] = None
    is_init: bool = False


# --------------------------------------------------------------------------- #
# UDP-Protokoll (empfaengt Antworten, ordnet sie per 'id' zu)
# --------------------------------------------------------------------------- #

class _MarstekProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: "MarstekUDPClient"):
        self._client = client

    def datagram_received(self, data: bytes, addr) -> None:
        self._client._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        logger.error("UDP Fehler: %s", exc, extra={"category": _Category.COMM})


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class MarstekUDPClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        init_policy: Optional[InitRetryPolicy] = None,
        runtime_policy: Optional[RuntimeRetryPolicy] = None,
        on_comm_state_change: Optional[Callable[[bool, bool], None]] = None,
        on_comm_fail_watchdog: Optional[Callable[[], Awaitable[None]]] = None,
        poll_interval_s: float = 0.05,
        min_inter_message_delay_s: float = 0.0,
    ):
        self._host = host
        self._port = port
        self._init_policy = init_policy or InitRetryPolicy()
        self._runtime_policy = runtime_policy or RuntimeRetryPolicy()
        self._on_comm_state_change = on_comm_state_change
        self._on_comm_fail_watchdog = on_comm_fail_watchdog
        self._poll_interval_s = poll_interval_s
        # Mindestabstand zwischen zwei GESENDETEN Nachrichten (jeder Kategorie),
        # um das Geraet nicht mit Nachrichten zu ueberrennen. Gilt NUR fuer die
        # Auswahl der naechsten STATUS-Nachricht - Control-Kommandos werden
        # davon nie aufgehalten und koennen sich jederzeit dazwischen einreihen.
        # 0 = deaktiviert (kein Mindestabstand).
        self._min_inter_message_delay_s = min_inter_message_delay_s
        self._last_send_monotonic: Optional[float] = None

        self._id_counter = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._control_queue: "asyncio.Queue[_QueueItem]" = asyncio.Queue()
        self._status_queue: "asyncio.Queue[_QueueItem]" = asyncio.Queue()

        self._transport: Optional[asyncio.DatagramTransport] = None
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closing = False

        self.comm_established = False
        self.comm_fail = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._transport, _ = await self._loop.create_datagram_endpoint(
            lambda: _MarstekProtocol(self),
            remote_addr=(self._host, self._port),
        )
        self._closing = False
        self._dispatcher_task = self._loop.create_task(self._dispatcher_loop())
        logger.info("UDP-Client verbunden mit %s:%s", self._host, self._port,
                    extra={"category": _Category.COMM})

    async def close(self) -> None:
        self._closing = True
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._transport:
            self._transport.close()

    # ------------------------------------------------------------------ #
    # Oeffentliche API
    # ------------------------------------------------------------------ #

    async def send_status(self, method: str, params: Optional[dict] = None,
                           *, retry_policy: Optional[RuntimeRetryPolicy] = None) -> Any:
        """Status-Abfrage (niedrige Prioritaet, kann von Control-Kommandos unterbrochen werden)."""
        return await self._enqueue(method, params or {}, CommandCategory.STATUS, retry_policy)

    async def send_control(self, method: str, params: Optional[dict] = None,
                            *, retry_policy: Optional[RuntimeRetryPolicy] = None) -> Any:
        """Control-Kommando (absolute Prioritaet)."""
        return await self._enqueue(method, params or {}, CommandCategory.CONTROL, retry_policy)

    async def send_init(self, method: str, params: Optional[dict] = None) -> Any:
        """Kommando waehrend der Erstinitialisierung (nutzt InitRetryPolicy, feste Werte)."""
        return await self._enqueue(method, params or {}, CommandCategory.STATUS,
                                    self._init_policy, is_init=True)

    # ------------------------------------------------------------------ #
    # Intern: Queueing & Dispatch
    # ------------------------------------------------------------------ #

    async def _enqueue(self, method, params, category, retry_policy, is_init=False) -> Any:
        future: asyncio.Future = self._loop.create_future()
        item = _QueueItem(method=method, params=params, category=category,
                           future=future, retry_policy=retry_policy, is_init=is_init)
        queue = self._control_queue if category == CommandCategory.CONTROL else self._status_queue
        await queue.put(item)
        return await future

    async def _dispatcher_loop(self) -> None:
        while not self._closing:
            item = await self._get_next_item()
            try:
                await self._process_command(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unerwarteter Fehler bei der Kommandoverarbeitung")

    async def _get_next_item(self) -> _QueueItem:
        # Control hat IMMER Vorrang, auch wenn eine Status-Abfrage schon laenger wartet.
        while True:
            if not self._control_queue.empty():
                return self._control_queue.get_nowait()
            if not self._status_queue.empty():
                if await self._wait_out_pacing_gap():
                    # Waehrend des Wartens ist ein Control-Kommando aufgetaucht ->
                    # von vorne pruefen, Control geht vor.
                    continue
                return self._status_queue.get_nowait()
            await asyncio.sleep(self._poll_interval_s)

    async def _wait_out_pacing_gap(self) -> bool:
        """Wartet, falls noetig, bis der konfigurierte Mindestabstand seit der
        letzten GESENDETEN Nachricht (egal welcher Kategorie) verstrichen ist.
        Gilt nur fuer die naechste STATUS-Nachricht - taucht waehrend des
        Wartens ein Control-Kommando auf, wird SOFORT (True) zurueckgegeben,
        damit der Aufrufer es vorlaesst, statt die volle Wartezeit abzusitzen.
        Gibt False zurueck, wenn kein Warten (mehr) noetig war."""
        if self._min_inter_message_delay_s <= 0 or self._last_send_monotonic is None:
            return False
        while True:
            remaining = self._min_inter_message_delay_s - (self._loop.time() - self._last_send_monotonic)
            if remaining <= 0:
                return False
            if not self._control_queue.empty():
                return True
            await asyncio.sleep(min(self._poll_interval_s, remaining))

    async def _drain_control_queue(self) -> None:
        """Wird zwischen zwei Retry-Versuchen einer Status-Abfrage aufgerufen:
        alle aktuell wartenden Control-Kommandos werden JETZT vollstaendig
        abgearbeitet (inkl. deren eigener Retry-Logik), bevor der naechste
        Status-Retry gestartet wird."""
        while not self._control_queue.empty():
            control_item = self._control_queue.get_nowait()
            logger.debug(
                "Control-Kommando '%s' wird vor dem naechsten Status-Retry abgearbeitet",
                control_item.method, extra={"category": _Category.CONTROL},
            )
            await self._process_command(control_item)

    # ------------------------------------------------------------------ #
    # Intern: Senden + Warten
    # ------------------------------------------------------------------ #

    async def _process_command(self, item: _QueueItem) -> None:
        policy = item.retry_policy or self._runtime_policy
        category_label = _Category.INIT if item.is_init else (
            _Category.CONTROL if item.category == CommandCategory.CONTROL else _Category.STATUS
        )

        attempt = 0
        last_error: Optional[BaseException] = None

        while attempt < policy.max_attempts:
            attempt += 1
            req_id = next(self._id_counter)
            request = {"id": req_id, "method": item.method, "params": item.params}
            response_future: asyncio.Future = self._loop.create_future()
            self._pending[req_id] = response_future

            timeout = policy.timeout_for_attempt(attempt)
            logger.debug(
                "SEND [%s] id=%s method=%s params=%s (Versuch %d/%d, Timeout %.1fs)",
                category_label.value, req_id, item.method, item.params,
                attempt, policy.max_attempts, timeout,
                extra={"category": category_label},
            )

            try:
                self._transport.sendto(json.dumps(request).encode("utf-8"))
                self._last_send_monotonic = self._loop.time()
                result = await asyncio.wait_for(response_future, timeout=timeout)
                logger.debug("RECV [%s] id=%s result=%s", category_label.value, req_id, result,
                             extra={"category": category_label})
                self._on_success()
                if not item.future.done():
                    item.future.set_result(result)
                return
            except asyncio.TimeoutError:
                logger.warning(
                    "TIMEOUT [%s] id=%s method=%s (Versuch %d/%d nach %.1fs)",
                    category_label.value, req_id, item.method, attempt, policy.max_attempts, timeout,
                    extra={"category": category_label},
                )
                last_error = MarstekCommunicationError(
                    f"Timeout bei '{item.method}' (Versuch {attempt}/{policy.max_attempts})"
                )
                # Bevor der naechste Retry gestartet wird (falls noch Versuche
                # uebrig sind): wartende Control-Kommandos haben Vorrang, da
                # das Geraet ohnehin nur einen Request auf einmal verarbeiten kann.
                if item.category == CommandCategory.STATUS and attempt < policy.max_attempts:
                    await self._drain_control_queue()
            except MarstekDeviceError as exc:
                logger.error("GERAETEFEHLER [%s] id=%s method=%s: %s",
                             category_label.value, req_id, item.method, exc,
                             extra={"category": category_label})
                # Geraetefehler ist keine Kommunikationsstoerung -> nicht retryen,
                # direkt an den Aufrufer durchreichen.
                self._on_success()  # Verbindung an sich funktioniert ja
                if not item.future.done():
                    item.future.set_exception(exc)
                return
            finally:
                self._pending.pop(req_id, None)

        # Alle Versuche ausgeschoepft
        self._on_failure(item, last_error)
        if not item.future.done():
            item.future.set_exception(last_error or MarstekCommunicationError("unbekannter Fehler"))

    # ------------------------------------------------------------------ #
    # Intern: Antwort-Zustellung
    # ------------------------------------------------------------------ #

    def _on_datagram(self, data: bytes) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Ungueltige/nicht parsebare Antwort erhalten: %r", data,
                            extra={"category": _Category.COMM})
            return

        resp_id = msg.get("id")
        future = self._pending.get(resp_id)
        if future is None or future.done():
            logger.debug("Antwort fuer unbekannte/bereits abgelaufene id=%s erhalten", resp_id,
                         extra={"category": _Category.COMM})
            return

        if "error" in msg:
            future.set_exception(MarstekDeviceError(msg["error"]))
        else:
            future.set_result(msg.get("result"))

    # ------------------------------------------------------------------ #
    # Intern: Verbindungsstatus
    # ------------------------------------------------------------------ #

    def _on_success(self) -> None:
        was_ok = self.comm_established and not self.comm_fail
        self.comm_established = True
        self.comm_fail = False
        if not was_ok:
            logger.info("Communication established", extra={"category": _Category.COMM})
            self._notify_state_change()

    def _on_failure(self, item: _QueueItem, error: Optional[BaseException]) -> None:
        was_failed = self.comm_fail
        self.comm_established = False
        self.comm_fail = True
        logger.error(
            "Communication Fail: '%s' nach allen Versuchen fehlgeschlagen (%s)",
            item.method, error, extra={"category": _Category.COMM},
        )
        if not was_failed:
            self._notify_state_change()
        if self._on_comm_fail_watchdog is not None:
            self._loop.create_task(self._invoke_watchdog())

    def _notify_state_change(self) -> None:
        if self._on_comm_state_change is not None:
            try:
                self._on_comm_state_change(self.comm_established, self.comm_fail)
            except Exception:
                logger.exception("Fehler im on_comm_state_change-Callback")

    async def _invoke_watchdog(self) -> None:
        try:
            result = self._on_comm_fail_watchdog()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Fehler im Watchdog-Callback")
