"""
test_udp_client.py

Testet udp_client.py gegen einen lokalen fake_marstek_server.py (kein
echtes Geraet noetig). Ausfuehren mit: pytest -v -s test_udp_client.py
"""

import asyncio
import logging
import time

import pytest

from udp_client import (
    MarstekUDPClient, InitRetryPolicy, RuntimeRetryPolicy,
    MarstekCommunicationError, MarstekDeviceError, setup_default_logging,
)
from fake_marstek_server import FakeMarstekServer, MethodBehavior

setup_default_logging(logging.DEBUG)


@pytest.mark.asyncio
async def test_basic_status_roundtrip():
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 77}))

    client = MarstekUDPClient("127.0.0.1", port, runtime_policy=RuntimeRetryPolicy(timeout_s=1.0, max_retries=2))
    await client.connect()
    try:
        result = await client.send_status("Bat.GetStatus", {"id": 0})
        assert result["soc"] == 77
        assert client.comm_established is True
        assert client.comm_fail is False
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_control_jumps_queue_between_status_retries_not_mid_wait():
    """Kernanforderung (korrigiert): Das Geraet kann nur einen Request auf
    einmal verarbeiten. Ein Control-Kommando darf daher NICHT mitten in eine
    laufende Status-Wartezeit hineinplatzen. Stattdessen: die Status-Abfrage
    laeuft in ihren ersten Timeout (Antwort wird 1x verschluckt), und ERST
    BEVOR der naechste Retry gestartet wird, wird das zwischenzeitlich
    wartende Control-Kommando abgearbeitet. Danach erst der Status-Retry,
    der dann (im guten Fall) erfolgreich ist."""
    server = FakeMarstekServer()
    port = await server.start()
    # Erster Aufruf von ES.GetStatus wird verschluckt (Timeout erzwungen),
    # der zweite (Retry) wird normal beantwortet.
    server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=1, result={"bat_soc": 55}))
    server.set_behavior("ES.SetMode", MethodBehavior(delay_s=0.0, result={"set_result": True}))

    status_timeout = 0.3
    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=status_timeout, max_retries=2),  # 3 Versuche
    )
    await client.connect()
    try:
        t_start = time.monotonic()
        status_task = asyncio.ensure_future(client.send_status("ES.GetStatus", {"id": 0}))
        await asyncio.sleep(0.05)  # Status-Anfrage 1. Versuch ist jetzt "in flight"

        control_task = asyncio.ensure_future(client.send_control(
            "ES.SetMode", {"id": 0, "config": {"mode": "Passive", "passive_cfg": {"power": 100, "cd_time": 60}}}
        ))

        control_result = await control_task
        control_elapsed = time.monotonic() - t_start

        # Das Control-Kommando darf ERST NACH Ablauf des ersten Status-Timeouts
        # gesendet worden sein (nicht sofort nach 0.05s mittendrin).
        assert control_elapsed >= status_timeout, (
            f"Control-Kommando wurde zu frueh gesendet ({control_elapsed:.2f}s < {status_timeout}s Status-Timeout)"
        )
        assert control_result["set_result"] is True

        status_result = await status_task
        assert status_result["bat_soc"] == 55
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_init_policy_increasing_timeout_and_gives_up():
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Marstek.GetDevice", MethodBehavior(drop_first_n=999))  # nie antworten

    fast_init_policy = InitRetryPolicy(base_timeout_s=0.1, timeout_increment_s=0.1, max_retries=2)  # 3 Versuche
    client = MarstekUDPClient("127.0.0.1", port, init_policy=fast_init_policy)
    await client.connect()
    try:
        t0 = time.monotonic()
        with pytest.raises(MarstekCommunicationError):
            await client.send_init("Marstek.GetDevice", {"ble_mac": "0"})
        elapsed = time.monotonic() - t0
        # erwartete Summe der Timeouts: 0.1 + 0.2 + 0.3 = 0.6s (+ etwas Overhead)
        assert 0.5 <= elapsed <= 1.5
        assert client.comm_fail is True
        assert client.comm_established is False
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_runtime_max_retries_triggers_comm_fail_and_watchdog():
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("ES.GetMode", MethodBehavior(drop_first_n=999))

    watchdog_calls = []

    async def watchdog():
        watchdog_calls.append(time.monotonic())

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=0.1, max_retries=2),  # 3 Versuche, je 0.1s
        on_comm_fail_watchdog=watchdog,
    )
    await client.connect()
    try:
        with pytest.raises(MarstekCommunicationError):
            await client.send_status("ES.GetMode", {"id": 0})
        await asyncio.sleep(0.05)  # Watchdog-Task Zeit geben
        assert client.comm_fail is True
        assert len(watchdog_calls) == 1
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_device_error_is_not_retried_and_does_not_mark_comm_fail():
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("DOD.SET", MethodBehavior(error={"code": -32602, "message": "Invalid params", "data": None}))

    client = MarstekUDPClient("127.0.0.1", port, runtime_policy=RuntimeRetryPolicy(timeout_s=1.0, max_retries=3))
    await client.connect()
    try:
        with pytest.raises(MarstekDeviceError):
            await client.send_control("DOD.SET", {"value": 999})
        # Geraetefehler (gueltige Antwort mit Fehlercode) ist keine Kommunikationsstoerung
        assert client.comm_established is True
        assert client.comm_fail is False
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_recovery_resets_comm_fail():
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=999))

    client = MarstekUDPClient("127.0.0.1", port, runtime_policy=RuntimeRetryPolicy(timeout_s=0.1, max_retries=1))
    await client.connect()
    try:
        with pytest.raises(MarstekCommunicationError):
            await client.send_status("ES.GetStatus", {"id": 0})
        assert client.comm_fail is True

        # Geraet "repariert" sich -> naechster Aufruf klappt
        server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=0, result={"bat_soc": 42}))
        result = await client.send_status("ES.GetStatus", {"id": 0})
        assert result["bat_soc"] == 42
        assert client.comm_fail is False
        assert client.comm_established is True
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_min_inter_message_delay_paces_consecutive_status_polls():
    """Kernanforderung: aufeinanderfolgende STATUS-Nachrichten (z.B. mehrere
    unabhaengige Poll-Loops, die zufaellig gleichzeitig feuern) muessen
    mindestens den konfigurierten Abstand einhalten."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 90}))
    server.set_behavior("ES.GetMode", MethodBehavior(result={"mode": "Auto"}))

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=1.0, max_retries=1),
        min_inter_message_delay_s=0.5,
    )
    await client.connect()
    try:
        t0 = time.monotonic()
        task1 = asyncio.ensure_future(client.send_status("Bat.GetStatus", {"id": 0}))
        task2 = asyncio.ensure_future(client.send_status("ES.GetMode", {"id": 0}))
        await task1
        t_second_start = None
        # zweite Anfrage muss erst nach dem Mindestabstand rausgehen
        await task2
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.5, f"Zweite Status-Nachricht kam zu frueh ({elapsed:.2f}s < 0.5s)"

        send_calls = [r for r in server.received]
        assert len(send_calls) == 2
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_control_bypasses_min_inter_message_delay():
    """Ein Control-Kommando darf NIE auf den Mindestabstand warten - es muss
    sich sofort dazwischen einreihen koennen, auch waehrend eine Status-
    Nachricht gerade auf den Mindestabstand wartet."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 90}))
    server.set_behavior("ES.SetMode", MethodBehavior(result={"set_result": True}))

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=1.0, max_retries=1),
        min_inter_message_delay_s=5.0,  # bewusst lang
    )
    await client.connect()
    try:
        t0 = time.monotonic()
        await client.send_status("Bat.GetStatus", {"id": 0})  # setzt _last_send_monotonic

        # sofort danach ein Control-Kommando -> darf NICHT 5s warten muessen
        control_result = await client.send_control(
            "ES.SetMode", {"id": 0, "config": {"mode": "Auto", "auto_cfg": {"enable": 1}}}
        )
        elapsed = time.monotonic() - t0
        assert control_result["set_result"] is True
        assert elapsed < 1.0, f"Control-Kommando wurde faelschlicherweise durch den Mindestabstand verzoegert ({elapsed:.2f}s)"
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_min_inter_message_delay_zero_disables_pacing():
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 90}))
    server.set_behavior("ES.GetMode", MethodBehavior(result={"mode": "Auto"}))

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=1.0, max_retries=1),
        min_inter_message_delay_s=0.0,
    )
    await client.connect()
    try:
        t0 = time.monotonic()
        await client.send_status("Bat.GetStatus", {"id": 0})
        await client.send_status("ES.GetMode", {"id": 0})
        elapsed = time.monotonic() - t0
        assert elapsed < 0.3, f"Ohne Pacing (0) sollte es schnell gehen, dauerte aber {elapsed:.2f}s"
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_init_commands_bypass_runtime_pacing_gate():
    """Kernanforderung (in der Praxis beobachtet und gemeldet): Init-Kommandos
    duerfen NICHT vom Laufzeit-Pacing-Gate (min_inter_message_delay_s)
    ausgebremst werden, da sie bereits ihre eigene Pause
    (init.inter_command_delay_s, gehandhabt in startup.py) haben. Sonst
    ueberlagern sich beide Mechanismen und die Init-Sequenz wird
    unbeabsichtigt langsamer als konfiguriert."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Marstek.GetDevice", MethodBehavior(result={"device": "VenusA", "ble_mac": "aabbcc"}))
    server.set_behavior("Wifi.GetStatus", MethodBehavior(result={"ssid": "test"}))

    client = MarstekUDPClient(
        "127.0.0.1", port,
        init_policy=InitRetryPolicy(base_timeout_s=1.0, timeout_increment_s=1.0, max_retries=1),
        min_inter_message_delay_s=5.0,  # bewusst gross, wie im gemeldeten Fall
    )
    await client.connect()
    try:
        t0 = time.monotonic()
        await client.send_init("Marstek.GetDevice", {"ble_mac": "0"})
        await client.send_init("Wifi.GetStatus", {"id": 0})
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, (
            f"Zwei aufeinanderfolgende Init-Kommandos duerfen NICHT durch das "
            f"5s-Laufzeit-Pacing-Gate ausgebremst werden, dauerte aber {elapsed:.2f}s"
        )
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_runtime_status_still_respects_pacing_gate_after_fix():
    """Regressionstest: normale Laufzeit-STATUS-Abfragen muessen weiterhin
    korrekt gepaced werden - nur Init-Kommandos sind ausgenommen."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 90}))
    server.set_behavior("ES.GetMode", MethodBehavior(result={"mode": "Auto"}))

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=1.0, max_retries=1),
        min_inter_message_delay_s=0.5,
    )
    await client.connect()
    try:
        t0 = time.monotonic()
        task1 = asyncio.ensure_future(client.send_status("Bat.GetStatus", {"id": 0}))
        task2 = asyncio.ensure_future(client.send_status("ES.GetMode", {"id": 0}))
        await task1
        await task2
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.5, f"Laufzeit-STATUS-Pacing haette weiterhin greifen muessen, dauerte nur {elapsed:.2f}s"
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_control_still_preempts_status_after_fix():
    """Regressionstest: Control-Preemption (unabhaengig vom Pacing-Gate)
    muss nach dem Fix weiterhin funktionieren."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 90}))
    server.set_behavior("ES.SetMode", MethodBehavior(result={"set_result": True}))

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=1.0, max_retries=1),
        min_inter_message_delay_s=5.0,
    )
    await client.connect()
    try:
        t0 = time.monotonic()
        await client.send_status("Bat.GetStatus", {"id": 0})
        control_result = await client.send_control(
            "ES.SetMode", {"id": 0, "config": {"mode": "Auto", "auto_cfg": {"enable": 1}}}
        )
        elapsed = time.monotonic() - t0
        assert control_result["set_result"] is True
        assert elapsed < 1.0, f"Control haette trotz Pacing-Gate sofort senden muessen ({elapsed:.2f}s)"
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_escalate_on_failure_false_does_not_trigger_comm_fail_or_watchdog():
    """Kernanforderung: escalate_on_failure=False bedeutet 'ein nach allen
    Versuchen weiterhin fehlender Response ist kein Verbindungsproblem' -
    kein Communication-Fail, kein Watchdog. Unabhaengig von max_retries.
    Das einzelne Kommando scheitert trotzdem fuer sich (Exception an den
    Aufrufer)."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=999))  # nie antworten

    watchdog_calls = []

    async def watchdog():
        watchdog_calls.append(1)

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=0.1, max_retries=0, escalate_on_failure=False),
        on_comm_fail_watchdog=watchdog,
    )
    await client.connect()
    try:
        with pytest.raises(MarstekCommunicationError):
            await client.send_status("ES.GetStatus", {"id": 0})
        await asyncio.sleep(0.1)  # Zeit fuer einen evtl. (unerwuenschten) Watchdog-Task geben
        assert client.comm_fail is False, "escalate_on_failure=False haette keinen Communication-Fail-Status setzen duerfen"
        assert len(watchdog_calls) == 0, "escalate_on_failure=False haette den Watchdog nicht ausloesen duerfen"
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_max_retry_zero_now_escalates_by_default():
    """Wichtiger Verhaltensunterschied ggue. vorheriger Version: max_retry=0
    ALLEIN loest jetzt (mit dem neuen Default escalate_on_failure=True)
    wieder eine Eskalation aus - die beiden Einstellungen sind entkoppelt.
    Wer die alte 'max_retry=0 = keine Eskalation'-Kopplung will, muss
    escalate_on_failure jetzt explizit auf False setzen."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=999))

    watchdog_calls = []

    async def watchdog():
        watchdog_calls.append(1)

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=0.1, max_retries=0),  # escalate_on_failure Default=True
        on_comm_fail_watchdog=watchdog,
    )
    await client.connect()
    try:
        with pytest.raises(MarstekCommunicationError):
            await client.send_status("ES.GetStatus", {"id": 0})
        await asyncio.sleep(0.05)
        assert client.comm_fail is True
        assert len(watchdog_calls) == 1
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_multiple_retries_with_escalation_disabled():
    """Kernanforderung: max_retries>0 (mehr Robustheit gegen einzelne
    Aussetzer) UND escalate_on_failure=False (keine Watchdog-Eskalation)
    lassen sich jetzt kombinieren - vorher ging das nur mit max_retry=0
    (also OHNE jede Wiederholung)."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=999))

    watchdog_calls = []

    async def watchdog():
        watchdog_calls.append(1)

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=0.1, max_retries=2, escalate_on_failure=False),
        on_comm_fail_watchdog=watchdog,
    )
    await client.connect()
    try:
        with pytest.raises(MarstekCommunicationError):
            await client.send_status("ES.GetStatus", {"id": 0})
        await asyncio.sleep(0.05)
        assert client.comm_fail is False
        assert len(watchdog_calls) == 0
        # trotzdem tatsaechlich 3 Versuche unternommen (max_retries=2 -> 3 attempts)
        calls = [r for r in server.received if r["method"] == "ES.GetStatus"]
        assert len(calls) == 3
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_max_retry_above_zero_still_triggers_comm_fail_and_watchdog():
    """Regressionstest: Default (escalate_on_failure=True) muss weiterhin
    normal eskalieren, wenn alle Versuche ausgeschoepft sind."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=999))

    watchdog_calls = []

    async def watchdog():
        watchdog_calls.append(1)

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=0.1, max_retries=1),
        on_comm_fail_watchdog=watchdog,
    )
    await client.connect()
    try:
        with pytest.raises(MarstekCommunicationError):
            await client.send_status("ES.GetStatus", {"id": 0})
        await asyncio.sleep(0.05)
        assert client.comm_fail is True
        assert len(watchdog_calls) == 1
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_escalate_on_failure_false_recovers_normally_on_next_success():
    """Nach einem toleriertem Fehlschlag (escalate_on_failure=False) muss
    ein nachfolgender erfolgreicher Aufruf ganz normal funktionieren."""
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("ES.GetStatus", MethodBehavior(drop_first_n=1, result={"bat_soc": 50}))

    client = MarstekUDPClient(
        "127.0.0.1", port,
        runtime_policy=RuntimeRetryPolicy(timeout_s=0.1, max_retries=0, escalate_on_failure=False),
    )
    await client.connect()
    try:
        with pytest.raises(MarstekCommunicationError):
            await client.send_status("ES.GetStatus", {"id": 0})
        assert client.comm_fail is False

        result = await client.send_status("ES.GetStatus", {"id": 0})
        assert result["bat_soc"] == 50
        assert client.comm_established is True
    finally:
        await client.close()
        server.stop()
