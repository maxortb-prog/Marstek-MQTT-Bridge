from config import MarstekConfig
from entities import build_entities


def test_build_entities_creates_expected_groups_and_no_duplicate_object_ids():
    cfg = MarstekConfig.from_dict({})
    bundle = build_entities(cfg, ble_mac="50cf14640fac", device_type="VenusC")

    assert bundle.hub_device.name == "Marstek System"
    assert bundle.battery_device.via_device == bundle.hub_device.identifiers[0]
    assert bundle.energy_status_device.via_device == bundle.hub_device.identifiers[0]
    assert bundle.energy_mode_device.via_device == bundle.hub_device.identifiers[0]
    assert bundle.energy_control_device.via_device == bundle.hub_device.identifiers[0]

    object_ids = list(bundle.entities.keys())
    assert len(object_ids) == len(set(object_ids)), "doppelte object_ids gefunden"

    # Kern-Entities vorhanden
    for expected in ["battery_soc", "es_bat_soc", "es_current_mode", "energy_mode",
                      "dod_value", "ble_broadcast", "led_ctrl", "udp_connection",
                      "communication_fail", "system_ready",
                      "passive_default_power", "passive_cd_time", "passive_cd_time_remaining"]:
        assert expected in bundle.entities, f"{expected} fehlt"


def test_energy_mode_select_excludes_manual():
    cfg = MarstekConfig.from_dict({})
    bundle = build_entities(cfg, ble_mac="aabbcc", device_type="VenusE")
    mode_entity = bundle.entities["energy_mode"]
    assert mode_entity.options == ["Auto", "AI", "Passive", "Ups"]
    assert "Manual" not in mode_entity.options


def test_field_maps_cover_documented_api_fields():
    cfg = MarstekConfig.from_dict({})
    bundle = build_entities(cfg, ble_mac="aabbcc", device_type="VenusC")

    for field in ["soc", "bat_temp", "bat_capacity", "rated_capacity", "charg_flag", "dischrg_flag"]:
        assert field in bundle.field_map_battery

    for field in ["bat_soc", "bat_cap", "pv_power", "ongrid_power", "offgrid_power",
                  "bat_power", "total_pv_energy", "total_grid_output_energy",
                  "total_grid_input_energy", "total_load_energy"]:
        assert field in bundle.field_map_es_status

    for field in ["mode", "ongrid_power", "offgrid_power", "bat_soc", "ct_state",
                  "a_power", "b_power", "c_power", "total_power", "input_energy", "output_energy"]:
        assert field in bundle.field_map_es_mode


def test_passive_number_ranges_come_from_config():
    cfg = MarstekConfig.from_dict({"controller": {"min_output_w": -1200, "max_output_w": 600},
                                    "passive_mode": {"max_cd_time": 1800}})
    bundle = build_entities(cfg, ble_mac="aabbcc", device_type="VenusC")
    power_entity = bundle.entities["passive_default_power"]
    assert power_entity.min_value == -1200
    assert power_entity.max_value == 600
    cd_entity = bundle.entities["passive_cd_time"]
    assert cd_entity.max_value == 1800
