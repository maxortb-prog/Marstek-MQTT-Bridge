import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from addon_options import apply_persisted_discovery, build_overrides
from config import MarstekConfig


def _minimal_options(**overrides):
    base = {"device_ip": "192.168.0.45", "device_udp_port": 30000}
    base.update(overrides)
    return base


def test_build_overrides_fills_all_expected_sections_with_defaults():
    overrides = build_overrides(_minimal_options())
    assert overrides["general"]["device_ip"] == "192.168.0.45"
    assert overrides["general"]["device_udp_port"] == 30000
    assert overrides["general"]["mqtt_host"] == "core-mosquitto"
    assert overrides["passive_mode"]["power"] == 800
    assert overrides["controller"]["min_send_interval_s"] == 30
    assert overrides["message_settings"]["max_retry"] == 3
    assert overrides["init"]["max_retries"] == 4
    assert overrides["dod"]["startup_value"] == 88
    assert overrides["shelly"]["power_topic"] == ""


def test_build_overrides_produces_a_config_that_passes_validation():
    overrides = build_overrides(_minimal_options())
    cfg = MarstekConfig.from_dict(overrides)  # wirft bei Validierungsfehlern
    assert cfg.get("general", "device_ip") == "192.168.0.45"


def test_mqtt_service_discovery_override_used_when_no_manual_host_set():
    overrides = build_overrides(
        _minimal_options(),
        mqtt_host_override="core-mosquitto.local",
        mqtt_port_override=1884,
        mqtt_username_override="addon_user",
        mqtt_password_override="secret",
    )
    assert overrides["general"]["mqtt_host"] == "core-mosquitto.local"
    assert overrides["general"]["mqtt_port"] == 1884
    assert overrides["general"]["mqtt_username"] == "addon_user"
    assert overrides["general"]["mqtt_password"] == "secret"


def test_manual_mqtt_host_in_options_wins_over_discovery():
    overrides = build_overrides(
        _minimal_options(mqtt_host="external.broker.example", mqtt_port=8883),
        mqtt_host_override="core-mosquitto.local",
        mqtt_port_override=1884,
    )
    assert overrides["general"]["mqtt_host"] == "external.broker.example"
    assert overrides["general"]["mqtt_port"] == 8883


def test_apply_persisted_discovery_fills_empty_fields_only(tmp_path):
    state_path = tmp_path / "marstek_state.yaml"
    state_path.write_text(yaml.safe_dump({
        "general": {"device_ble_mac": "AABBCCDDEEFF", "device_type": "VenusE"}
    }))

    overrides = build_overrides(_minimal_options())  # ble_mac/device_type leer
    apply_persisted_discovery(overrides, state_path)
    assert overrides["general"]["device_ble_mac"] == "AABBCCDDEEFF"
    assert overrides["general"]["device_type"] == "VenusE"


def test_apply_persisted_discovery_does_not_override_explicit_option():
    overrides = build_overrides(_minimal_options(device_ble_mac="112233445566"))

    # State-Datei existiert nicht -> darf nichts aendern
    apply_persisted_discovery(overrides, Path("/nonexistent/path.yaml"))
    assert overrides["general"]["device_ble_mac"] == "112233445566"


def test_apply_persisted_discovery_missing_file_is_noop(tmp_path):
    overrides = build_overrides(_minimal_options())
    result = apply_persisted_discovery(overrides, tmp_path / "does_not_exist.yaml")
    assert result["general"]["device_ble_mac"] == ""


def test_full_options_set_maps_correctly_and_validates():
    options = _minimal_options(
        device_ble_mac="", device_type="",
        mqtt_discovery_prefix="homeassistant", mqtt_base_topic="Marstek-Bridge-Control",
        mqtt_suggested_area="Keller",
        bat_status_interval_s=1800, es_mode_interval_s=600, es_status_interval_s=120,
        passive_power=100, passive_cd_time=120, passive_max_cd_time=1800,
        controller_deadzone_w=30, controller_min_setpoint_change_w=40,
        controller_max_step_w=100, controller_min_output_w=-1200, controller_max_output_w=600,
        controller_min_send_interval_s=20,
        message_settings_max_retry=5, message_settings_timeout_s=1.5,
        init_base_timeout_s=1.0, init_timeout_increment_s=3.0, init_max_retries=6,
        init_inter_command_delay_s=2.0,
        dod_startup_value=50, led_startup_state=1, ble_block_startup_enable=1,
        shelly_power_topic="shellies/em/power",
    )
    overrides = build_overrides(options)
    cfg = MarstekConfig.from_dict(overrides)
    assert cfg.get("general", "mqtt_suggested_area") == "Keller"
    assert cfg.get("status_polling", "es_status_interval_s") == 120
    assert cfg.get("controller", "min_send_interval_s") == 20
    assert cfg.get("shelly", "power_topic") == "shellies/em/power"
