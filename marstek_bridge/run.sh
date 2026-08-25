#!/usr/bin/with-contenv bashio
# ---------------------------------------------------------------------------
# Ermittelt (falls verfuegbar) die MQTT-Zugangsdaten des Mosquitto-Add-ons
# per Supervisor-Service-Discovery und reicht sie als Umgebungsvariablen an
# main.py weiter. Manuell in den Add-on-Optionen gesetzte MQTT-Werte haben
# in main.py/addon_options.py trotzdem Vorrang vor diesen Werten.
# ---------------------------------------------------------------------------

set -e

if bashio::services.available "mqtt"; then
    bashio::log.info "MQTT-Service-Discovery verfuegbar - lese Zugangsdaten"
    export MARSTEK_MQTT_HOST=$(bashio::services "mqtt" "host")
    export MARSTEK_MQTT_PORT=$(bashio::services "mqtt" "port")
    export MARSTEK_MQTT_USER=$(bashio::services "mqtt" "username")
    export MARSTEK_MQTT_PASSWORD=$(bashio::services "mqtt" "password")
else
    bashio::log.warning "Kein MQTT-Service per Discovery gefunden - es werden nur die " \
        "manuell gesetzten Add-on-Optionen verwendet (mqtt_host/mqtt_port/mqtt_username/mqtt_password)"
fi

bashio::log.info "Starte Marstek MQTT Bridge..."
exec python3 /app/main.py
