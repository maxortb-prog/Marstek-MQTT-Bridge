import copy
import pytest
import yaml

from config import MarstekConfig, ConfigError, DEFAULT_CONFIG


def test_load_missing_file_uses_defaults(tmp_path):
    cfg = MarstekConfig.load(tmp_path / "does_not_exist.yaml")
    assert cfg.get("general", "device_udp_port") == 30000
    assert cfg.get("passive_mode", "power") == 800


def test_load_partial_user_config_merges_with_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"general": {"device_ip": "10.0.0.5"}}))
    cfg = MarstekConfig.load(path)
    assert cfg.get("general", "device_ip") == "10.0.0.5"
    # Rest bleibt Default
    assert cfg.get("general", "device_udp_port") == 30000
    assert cfg.get("controller", "deadzone_w") == 40


def test_set_and_persist_writes_file_and_survives_reload(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = MarstekConfig.load(path)  # existiert nicht -> Defaults, aber Pfad gemerkt
    cfg.save(path)  # initial anlegen

    cfg2 = MarstekConfig.load(path)
    cfg2.set_and_persist("general", "device_ble_mac", value="AABBCCDDEEFF")
    cfg2.set_and_persist("general", "device_type", value="VenusE")

    cfg3 = MarstekConfig.load(path)
    assert cfg3.get("general", "device_ble_mac") == "AABBCCDDEEFF"
    assert cfg3.get("general", "device_type") == "VenusE"
    # unveraendert:
    assert cfg3.get("general", "mqtt_host") == "core-mosquitto"


def test_validation_rejects_bad_values():
    with pytest.raises(ConfigError):
        MarstekConfig.from_dict({"message_settings": {"max_retry": 99}})
    with pytest.raises(ConfigError):
        MarstekConfig.from_dict({"controller": {"min_send_interval_s": 120}})
    with pytest.raises(ConfigError):
        MarstekConfig.from_dict({"controller": {"min_output_w": 900, "max_output_w": 800}})
    with pytest.raises(ConfigError):
        MarstekConfig.from_dict({"passive_mode": {"max_cd_time": 4000}})
    with pytest.raises(ConfigError):
        MarstekConfig.from_dict({"dod": {"startup_value": 10}})


def test_power_zero_is_valid_and_means_no_discharge():
    """passive_mode.power ist jetzt ein Entlade-Deckel: 0 ist eine gueltige,
    bewusste Einstellung (Entladen im Passive-Mode vollstaendig sperren)."""
    cfg = MarstekConfig.from_dict({"passive_mode": {"power": 0}})
    assert cfg.get("passive_mode", "power") == 0


def test_negative_power_is_rejected():
    with pytest.raises(ConfigError):
        MarstekConfig.from_dict({"passive_mode": {"power": -10}})


def test_default_config_is_not_mutated_by_from_dict():
    original = copy.deepcopy(DEFAULT_CONFIG)
    MarstekConfig.from_dict({"general": {"device_ip": "1.2.3.4"}})
    assert DEFAULT_CONFIG == original
