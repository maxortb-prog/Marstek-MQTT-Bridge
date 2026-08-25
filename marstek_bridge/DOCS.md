# Marstek MQTT Bridge

Bruecke zwischen einem Marstek Energiespeicher (lokale UDP-API, siehe
"Marstek Device Open API") und Home Assistant per MQTT-Discovery.

## Voraussetzungen

- Der Marstek Open-API-Modus muss in der Marstek-App aktiviert sein
  (siehe Kapitel 2.2.1 der Marstek-API-Dokumentation), mit fester IP-Adresse
  des Geraets im lokalen Netz.
- Ein laufender MQTT-Broker. Empfohlen: das offizielle "Mosquitto broker"-
  Add-on. Wird es verwendet, erkennt dieses Add-on die Zugangsdaten
  automatisch (Service-Discovery) - es muss dann nichts unter
  `mqtt_host`/`mqtt_port`/... eingetragen werden.

## Konfiguration

### Pflichtfelder

| Option | Beschreibung |
|---|---|
| `device_ip` | IP-Adresse des Marstek-Geraets im LAN |
| `device_udp_port` | UDP-Port, wie in der Marstek-App unter "Open API" konfiguriert (Standard 30000) |

### Automatisch erkannt (leer lassen)

| Option | Beschreibung |
|---|---|
| `device_ble_mac` | Wird beim ersten Start per `BLE.GetStatus` ermittelt und dauerhaft gemerkt |
| `device_type` | Wird beim ersten Start per `Marstek.GetDevice` ermittelt und dauerhaft gemerkt |

### MQTT (nur bei externem Broker noetig)

| Option | Beschreibung |
|---|---|
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | Nur ausfuellen, wenn **kein** Mosquitto-Add-on mit Service-Discovery verwendet wird |
| `mqtt_discovery_prefix` | Standard `homeassistant`, i.d.R. nicht aendern |
| `mqtt_base_topic` | Topic-Praefix fuer alle State-/Command-Topics dieses Add-ons |
| `mqtt_suggested_area` | Vorgeschlagener HA-Bereich fuer alle erzeugten Geraete |

### Poll-Intervalle (Sekunden)

| Option | Standard | Bedeutung |
|---|---|---|
| `bat_status_interval_s` | 3600 (60 min) | `Bat.GetStatus` |
| `es_mode_interval_s` | 900 (15 min) | `ES.GetMode` |
| `es_status_interval_s` | 300 (5 min) | `ES.GetStatus` |

### Passive-Mode Regler ("Controller")

Traeger Filter (Totzone, Slew-Rate, Mindeständerung, Hold-off), der aus
einer externen Leistungsmessung (`shelly_power_topic`) einen ruhigen
Passive-Mode-Sollwert fuer den Marstek berechnet, statt auf jede Spitze zu
reagieren.

| Option | Standard | Bedeutung |
|---|---|---|
| `controller_deadzone_w` | 40 | Rauschunterdrueckung, W |
| `controller_min_setpoint_change_w` | 50 | Mindeständerung vor erneutem Senden, W |
| `controller_max_step_w` | 125 | Max. Leistungsschritt pro Zyklus, W |
| `controller_min_output_w` | -1500 | Ladegrenze (negativ = Laden) |
| `controller_max_output_w` | 800 | Einspeisegrenze |
| `controller_min_send_interval_s` | 30 | Mindestabstand zwischen zwei Sendungen, 0-60s (0 = kein Mindestabstand) |
| `shelly_power_topic` | (leer) | MQTT-Topic einer externen Leistungsmessung (z.B. Shelly EM). Leer = Regler deaktiviert, nur manuelle Passive-Steuerung ueber HA moeglich |

### Nachrichten-/Retry-Verhalten

| Option | Standard | Bedeutung |
|---|---|---|
| `message_max_retry` | 3 | Max. Wiederholungen im laufenden Betrieb (0-10) |
| `message_timeout_s` | 1.0 | Timeout pro Versuch im laufenden Betrieb |
| `init_base_timeout_s` / `init_timeout_increment_s` / `init_max_retries` | 2.0 / 5.0 / 4 | Timeout-Schema fuer die Erstinitialisierung (steigt pro Versuch) |

## Entities in Home Assistant

Nach dem ersten erfolgreichen Start erscheinen automatisch mehrere Geraete:
**Marstek System**, **Marstek Battery**, **Marstek Energy Status**,
**Marstek Energy Mode**, **Marstek Energy Control**.

Wichtige Status-Entities fuer Automatisierungen:

- `binary_sensor.system_ready` - Initialisierung abgeschlossen, Bridge im
  Pollzyklus. Fuer "warte bis die Bridge bereit ist"-Automatisierungen.
- `binary_sensor.communication_fail` (`device_class: problem`) - geht auf
  **ON**, wenn ein Kommando nach allen konfigurierten Wiederholversuchen
  ohne Antwort bleibt (Geraet haengt/antwortet nicht). Empfohlener
  Ausloeser fuer eine Push-Benachrichtigung.
- `binary_sensor.udp_connection` (`device_class: connectivity`) - laufender
  Verbindungsstatus.

## Watchdog / automatischer Neustart

Erschoepft ein Kommando im laufenden Betrieb alle Wiederholversuche, beendet
sich der Bridge-Prozess kontrolliert mit Exit-Code 1. Damit Home Assistant
das Add-on dann automatisch neu startet, muss unter **Einstellungen ->
Add-ons -> Marstek MQTT Bridge -> Info** der Schalter **"Watchdog"**
aktiviert werden.

## Bekannte Einschraenkungen

- Der `Manual`-Modus von `ES.SetMode` wird bewusst nicht unterstuetzt
  (nur Auto/AI/Passive/Ups).
- `cd_time` (Passive-Mode-Countdown) wird vom Geraet nicht zurückgemeldet
  und daher lokal im Add-on mitgezaehlt - nach einem Neustart des Add-ons
  beginnt die Anzeige neu.
