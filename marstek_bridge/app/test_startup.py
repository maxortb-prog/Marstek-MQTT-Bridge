import pytest

from config import MarstekConfig
from udp_client import MarstekUDPClient, InitRetryPolicy, MarstekCommunicationError
from fake_marstek_server import FakeMarstekServer, MethodBehavior
from startup import run_startup_sequence


def _fast_init_config(overrides=None):
    base = {
        "general": {"device_ble_mac": "", "device_type": ""},
        "init": {"base_timeout_s": 0.2, "timeout_increment_s": 0.1, "max_retries": 2,
                 "inter_command_delay_s": 0.0},  # kein Sleep zwischen Kommandos -> schneller Test
    }
    if overrides:
        base = {**base, **overrides}
    return MarstekConfig.from_dict(base)


def _setup_server_happy_path(server: FakeMarstekServer):
    server.set_behavior("Marstek.GetDevice", MethodBehavior(
        result={"device": "VenusC", "ver": 111, "wifi_mac": "aabbcc", "wifi_name": "MY_HOME", "ip": "192.168.1.11"}))
    server.set_behavior("Wifi.GetStatus", MethodBehavior(result={"wifi_mac": "aabbcc", "ssid": "MY_HOME", "rssi": -55}))
    server.set_behavior("BLE.GetStatus", MethodBehavior(result={"state": "connect", "ble_mac": "50cf14640fac"}))
    server.set_behavior("Bat.GetStatus", MethodBehavior(result={"soc": 90, "bat_temp": 25.0}))
    server.set_behavior("ES.GetStatus", MethodBehavior(result={"bat_soc": 91, "pv_power": 0}))
    server.set_behavior("ES.GetMode", MethodBehavior(result={"mode": "Auto"}))
    server.set_behavior("DOD.SET", MethodBehavior(result={"set_result": True}))
    server.set_behavior("Ble.Adv", MethodBehavior(result={"set_result": True}))
    server.set_behavior("Led.Ctrl", MethodBehavior(result={"set_result": True}))


@pytest.mark.asyncio
async def test_full_startup_sequence_discovers_and_persists_ble_mac_and_device_type(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    _setup_server_happy_path(server)

    config_path = tmp_path / "config.yaml"
    cfg = _fast_init_config()
    cfg.save(config_path)
    cfg = MarstekConfig.load(config_path)  # jetzt mit Pfad, damit set_and_persist schreibt

    client = MarstekUDPClient("127.0.0.1", port,
                               init_policy=InitRetryPolicy(base_timeout_s=0.2, timeout_increment_s=0.1, max_retries=2))
    await client.connect()
    try:
        result = await run_startup_sequence(client, cfg)

        assert result.device_type == "VenusC"
        assert result.ble_mac == "50cf14640fac"
        assert result.dod_set_result is True
        assert result.ble_adv_set_result is True
        assert result.led_set_result is True
        assert result.battery_status["soc"] == 90
        assert result.es_status["bat_soc"] == 91
        assert result.es_mode["mode"] == "Auto"

        # persistiert?
        reloaded = MarstekConfig.load(config_path)
        assert reloaded.get("general", "device_ble_mac") == "50cf14640fac"
        assert reloaded.get("general", "device_type") == "VenusC"
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_startup_respects_preconfigured_ble_mac_and_device_type(tmp_path):
    """Wenn ble_mac/device_type schon in der Config stehen, sollen die
    ermittelten Werte des Geraets nicht ueberschrieben und NICHT erneut
    gespeichert werden (kein unnoetiger Schreibzugriff)."""
    server = FakeMarstekServer()
    port = await server.start()
    _setup_server_happy_path(server)

    config_path = tmp_path / "config.yaml"
    cfg = _fast_init_config({
        "general": {"device_ble_mac": "AABBCCDDEEFF", "device_type": "VenusE"},
        "init": {"base_timeout_s": 0.2, "timeout_increment_s": 0.1, "max_retries": 2, "inter_command_delay_s": 0.0},
    })
    cfg.save(config_path)
    cfg = MarstekConfig.load(config_path)

    client = MarstekUDPClient("127.0.0.1", port,
                               init_policy=InitRetryPolicy(base_timeout_s=0.2, timeout_increment_s=0.1, max_retries=2))
    await client.connect()
    try:
        result = await run_startup_sequence(client, cfg)
        # die in der Config vorgegebenen Werte gewinnen
        assert result.device_type == "VenusE"
        assert result.ble_mac == "AABBCCDDEEFF"
    finally:
        await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_startup_raises_and_aborts_on_device_offline(tmp_path):
    server = FakeMarstekServer()
    port = await server.start()
    server.set_behavior("Marstek.GetDevice", MethodBehavior(drop_first_n=999))  # nie antworten

    config_path = tmp_path / "config.yaml"
    cfg = _fast_init_config()
    cfg.save(config_path)
    cfg = MarstekConfig.load(config_path)

    client = MarstekUDPClient("127.0.0.1", port,
                               init_policy=InitRetryPolicy(base_timeout_s=0.1, timeout_increment_s=0.1, max_retries=1))
    await client.connect()
    try:
        with pytest.raises(MarstekCommunicationError):
            await run_startup_sequence(client, cfg)
        assert client.comm_fail is True
    finally:
        await client.close()
        server.stop()
