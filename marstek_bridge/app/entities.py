"""
entities.py

Definiert die HA-'Geraete-Gruppen' (HADevice) und alle HAEntity-Objekte,
die die Marstek-API-Felder auf Home-Assistant-Entities abbilden.

Geraete-Gruppen (siehe Projektspezifikation):
    - Marstek System          (Marstek.GetDevice, Wifi.GetStatus, BLE.GetStatus,
                                DOD, Ble_block, Led_Ctrl, Communication-Status)
    - Marstek Battery         (Bat.GetStatus)
    - Marstek Energy Status   (ES.GetStatus)
    - Marstek Energy Mode     (ES.GetMode)
    - Marstek Energy Control  (ES.SetMode: Auto/AI/Passive/Ups + Passive-Parameter)

Alle Untergruppen haengen per via_device am 'Marstek System'-Hub, damit sie
in HA als zusammengehoerig dargestellt werden.

Dieses Modul erzeugt nur die Entity-Objekte (inkl. der Zuordnung
API-Feldname -> Entity), es kennt weder MQTT noch UDP - reine Datenstruktur.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import MarstekConfig
from mqtt_ha import HADevice, HAEntity


@dataclass
class EntityBundle:
    hub_device: HADevice
    battery_device: HADevice
    energy_status_device: HADevice
    energy_mode_device: HADevice
    energy_control_device: HADevice

    # object_id -> HAEntity, fuer die Registrierung bei der Bridge
    entities: dict

    # API-Feldname -> object_id, gruppiert nach API-Aufruf, fuer die Poll-Loops
    field_map_battery: dict
    field_map_es_status: dict
    field_map_es_mode: dict


def build_entities(cfg: MarstekConfig, ble_mac: str, device_type: str) -> EntityBundle:
    area = cfg.get("general", "mqtt_suggested_area", default="Marstek")
    hub_id = f"marstek_{ble_mac or 'unknown'}"

    hub = HADevice(identifiers=(hub_id,), name="Marstek System", model=device_type or None,
                    suggested_area=area)
    battery_dev = HADevice(identifiers=(f"{hub_id}_battery",), name="Marstek Battery",
                            suggested_area=area, via_device=hub_id)
    es_status_dev = HADevice(identifiers=(f"{hub_id}_energy_status",), name="Marstek Energy Status",
                              suggested_area=area, via_device=hub_id)
    es_mode_dev = HADevice(identifiers=(f"{hub_id}_energy_mode",), name="Marstek Energy Mode",
                            suggested_area=area, via_device=hub_id)
    es_control_dev = HADevice(identifiers=(f"{hub_id}_energy_control",), name="Marstek Energy Control",
                               suggested_area=area, via_device=hub_id)

    entities: dict = {}

    def add(entity: HAEntity) -> HAEntity:
        entities[entity.object_id] = entity
        return entity

    # ---------------------------------------------------------------- #
    # Marstek System (init-only Diagnose-Infos + Steuerung + Comm-Status)
    # ---------------------------------------------------------------- #
    add(HAEntity("sensor", "device_model", "Device Model", hub, entity_category="diagnostic"))
    add(HAEntity("sensor", "device_fw_version", "Firmware Version", hub, entity_category="diagnostic"))
    add(HAEntity("sensor", "wifi_ip", "WiFi IP", hub, entity_category="diagnostic"))
    add(HAEntity("sensor", "wifi_ssid", "WiFi SSID", hub, entity_category="diagnostic"))
    add(HAEntity("sensor", "wifi_rssi", "WiFi Signal", hub, unit_of_measurement="dBm",
                 device_class="signal_strength", state_class="measurement", entity_category="diagnostic"))
    add(HAEntity("sensor", "wifi_mac", "WiFi MAC", hub, entity_category="diagnostic"))
    add(HAEntity("sensor", "ble_mac", "Bluetooth MAC", hub, entity_category="diagnostic"))
    add(HAEntity("sensor", "ble_state", "Bluetooth State", hub, entity_category="diagnostic"))

    add(HAEntity("binary_sensor", "udp_connection", "UDP Connection", hub, device_class="connectivity"))
    add(HAEntity("binary_sensor", "communication_fail", "Communication Fail", hub, device_class="problem"))
    add(HAEntity("binary_sensor", "system_ready", "System Ready", hub, icon="mdi:check-network-outline"))

    add(HAEntity("number", "dod_value", "DOD", hub, min_value=30, max_value=88, step=1,
                 icon="mdi:battery-arrow-down", entity_category="config"))
    add(HAEntity("switch", "ble_broadcast", "Bluetooth Broadcasting", hub,
                 icon="mdi:bluetooth", entity_category="config"))
    add(HAEntity("switch", "led_ctrl", "Panel LED", hub, icon="mdi:led-on", entity_category="config"))

    # ---------------------------------------------------------------- #
    # Marstek Battery (Bat.GetStatus, periodisch)
    # ---------------------------------------------------------------- #
    field_map_battery = {}

    def add_battery(field_name, object_id, name, **kwargs):
        add(HAEntity("sensor", object_id, name, battery_dev, **kwargs))
        field_map_battery[field_name] = object_id

    add_battery("soc", "battery_soc", "Battery SOC", unit_of_measurement="%",
                device_class="battery", state_class="measurement")
    add_battery("bat_temp", "battery_temp", "Battery Temperature", unit_of_measurement="°C",
                device_class="temperature", state_class="measurement")
    add_battery("bat_capacity", "battery_capacity", "Battery Capacity", unit_of_measurement="Wh",
                device_class="energy_storage", state_class="measurement")
    add_battery("rated_capacity", "battery_rated_capacity", "Battery Rated Capacity",
                unit_of_measurement="Wh", entity_category="diagnostic")
    add(HAEntity("binary_sensor", "battery_charge_allowed", "Charging Allowed", battery_dev))
    field_map_battery["charg_flag"] = "battery_charge_allowed"
    add(HAEntity("binary_sensor", "battery_discharge_allowed", "Discharging Allowed", battery_dev))
    field_map_battery["dischrg_flag"] = "battery_discharge_allowed"

    # ---------------------------------------------------------------- #
    # Marstek Energy Status (ES.GetStatus, periodisch)
    # ---------------------------------------------------------------- #
    field_map_es_status = {}

    def add_es_status(field_name, object_id, name, **kwargs):
        add(HAEntity("sensor", object_id, name, es_status_dev, **kwargs))
        field_map_es_status[field_name] = object_id

    add_es_status("bat_soc", "es_bat_soc", "Battery SOC (ES)", unit_of_measurement="%",
                  device_class="battery", state_class="measurement")
    add_es_status("bat_cap", "es_bat_cap", "Battery Capacity (ES)", unit_of_measurement="Wh")
    add_es_status("pv_power", "es_pv_power", "PV Power", unit_of_measurement="W",
                  device_class="power", state_class="measurement")
    add_es_status("ongrid_power", "es_ongrid_power", "Grid-Tied Power", unit_of_measurement="W",
                  device_class="power", state_class="measurement")
    add_es_status("offgrid_power", "es_offgrid_power", "Off-Grid Power", unit_of_measurement="W",
                  device_class="power", state_class="measurement")
    add_es_status("bat_power", "es_bat_power", "Battery Power", unit_of_measurement="W",
                  device_class="power", state_class="measurement")
    add_es_status("total_pv_energy", "es_total_pv_energy", "Total PV Energy",
                  unit_of_measurement="Wh", device_class="energy", state_class="total_increasing")
    add_es_status("total_grid_output_energy", "es_total_grid_output_energy", "Total Grid Output Energy",
                  unit_of_measurement="Wh", device_class="energy", state_class="total_increasing")
    add_es_status("total_grid_input_energy", "es_total_grid_input_energy", "Total Grid Input Energy",
                  unit_of_measurement="Wh", device_class="energy", state_class="total_increasing")
    add_es_status("total_load_energy", "es_total_load_energy", "Total Load Energy",
                  unit_of_measurement="Wh", device_class="energy", state_class="total_increasing")

    # ---------------------------------------------------------------- #
    # Marstek Energy Mode (ES.GetMode, periodisch)
    # ---------------------------------------------------------------- #
    field_map_es_mode = {}

    def add_es_mode(field_name, object_id, name, **kwargs):
        add(HAEntity("sensor", object_id, name, es_mode_dev, **kwargs))
        field_map_es_mode[field_name] = object_id

    add_es_mode("mode", "es_current_mode", "Current Mode")
    add_es_mode("ongrid_power", "es_mode_ongrid_power", "Grid-Tied Power (Mode)", unit_of_measurement="W",
                device_class="power", state_class="measurement")
    add_es_mode("offgrid_power", "es_mode_offgrid_power", "Off-Grid Power (Mode)", unit_of_measurement="W",
                device_class="power", state_class="measurement")
    add_es_mode("bat_soc", "es_mode_bat_soc", "Battery SOC (Mode)", unit_of_measurement="%",
                device_class="battery", state_class="measurement")
    add(HAEntity("binary_sensor", "ct_connected", "CT Connected", es_mode_dev))
    field_map_es_mode["ct_state"] = "ct_connected"
    add_es_mode("a_power", "es_phase_a_power", "Phase A Power", unit_of_measurement="W",
                device_class="power", state_class="measurement")
    add_es_mode("b_power", "es_phase_b_power", "Phase B Power", unit_of_measurement="W",
                device_class="power", state_class="measurement")
    add_es_mode("c_power", "es_phase_c_power", "Phase C Power", unit_of_measurement="W",
                device_class="power", state_class="measurement")
    add_es_mode("total_power", "es_ct_total_power", "CT Total Power", unit_of_measurement="W",
                device_class="power", state_class="measurement")
    add_es_mode("input_energy", "es_ct_input_energy", "CT Cumulative Input Energy",
                unit_of_measurement="Wh", device_class="energy", state_class="total_increasing")
    add_es_mode("output_energy", "es_ct_output_energy", "CT Cumulative Output Energy",
                unit_of_measurement="Wh", device_class="energy", state_class="total_increasing")

    # ---------------------------------------------------------------- #
    # Marstek Energy Control (ES.SetMode: Auto/AI/Passive/Ups)
    # ---------------------------------------------------------------- #
    # Nur die vom Nutzer explizit gewuenschten Modi (Manual bewusst ausgeklammert)
    add(HAEntity("select", "energy_mode", "Energy Mode", es_control_dev,
                 options=["Auto", "AI", "Passive", "Ups"]))
    add(HAEntity("number", "passive_default_power", "Passive Max Discharge Power", es_control_dev,
                 unit_of_measurement="W", icon="mdi:battery-arrow-up",
                 min_value=0,
                 max_value=cfg.get("controller", "max_output_w", default=800), step=10))
    add(HAEntity("number", "passive_cd_time", "Passive Countdown Time", es_control_dev,
                 unit_of_measurement="s", min_value=1,
                 max_value=cfg.get("passive_mode", "max_cd_time", default=3600), step=10))
    add(HAEntity("sensor", "passive_cd_time_remaining", "Passive Countdown Remaining",
                 es_control_dev, unit_of_measurement="s", state_class="measurement"))
    add(HAEntity("sensor", "passive_last_sent_power", "Passive Last Sent Power",
                 es_control_dev, unit_of_measurement="W", device_class="power",
                 state_class="measurement"))
    add(HAEntity("number", "shelly_debounce_time_s", "Shelly Input Debounce Time", es_control_dev,
                 unit_of_measurement="s", icon="mdi:sine-wave", min_value=0, max_value=300, step=1))
    add(HAEntity("button", "passive_resend", "Resend Passive Command", es_control_dev,
                 icon="mdi:refresh", entity_category="config"))

    return EntityBundle(
        hub_device=hub,
        battery_device=battery_dev,
        energy_status_device=es_status_dev,
        energy_mode_device=es_mode_dev,
        energy_control_device=es_control_dev,
        entities=entities,
        field_map_battery=field_map_battery,
        field_map_es_status=field_map_es_status,
        field_map_es_mode=field_map_es_mode,
    )
