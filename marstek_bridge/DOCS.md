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
- **Home Assistant Supervisor 2025.10 oder neuer**, damit die gruppierten
  Konfigurationsabschnitte (siehe unten) korrekt in der Add-on-UI angezeigt
  werden.

## Konfiguration

Die Optionen sind - seit HA-Supervisor-Unterstuetzung fuer verschachtelte
Add-on-Optionen - in echte Abschnitte gruppiert, die als eigene
Unterformulare in der Add-on-Konfigurations-UI erscheinen.

### Abschnitt "Marstek Device"

| Option | Standard | Beschreibung |
|---|---|---|
| `device_ip` | `192.168.0.45` | IP-Adresse des Marstek-Geraets im LAN (Pflichtfeld) |
| `device_udp_port` | `30000` | UDP-Port, wie in der Marstek-App unter "Open API" konfiguriert (Pflichtfeld) |
| `device_ble_mac` | (leer) | Automatisch erkannt: wird beim ersten Start per `BLE.GetStatus` ermittelt und dauerhaft gemerkt |
| `device_type` | (leer) | Automatisch erkannt: wird beim ersten Start per `Marstek.GetDevice` ermittelt und dauerhaft gemerkt |
| `mqtt_discovery_prefix` | `homeassistant` | I.d.R. nicht aendern |
| `mqtt_base_topic` | `Marstek-Bridge-Control` | Topic-Praefix fuer alle State-/Command-Topics dieses Add-ons |
| `mqtt_suggested_area` | `Marstek` | Vorgeschlagener HA-Bereich fuer alle erzeugten Geraete |

### Abschnitt "MQTT Settings"

Nur ausfuellen, wenn **kein** Mosquitto-Add-on mit Service-Discovery
verwendet wird.

| Option | Bedeutung |
|---|---|
| `mqtt_host` | Broker-Host |
| `mqtt_username` | Broker-Benutzername |
| `mqtt_password` | Broker-Passwort |
| `mqtt_port` (liegt bewusst ausserhalb dieser Gruppe) | Broker-Port. Leer lassen fuer automatische Erkennung - ein vorbelegter Default wuerde die Service-Discovery verhindern. |

### Abschnitt "Scanrate for Statuscalls"

Der jeweils erste Poll-Zyklus jeder Status-Abfrage startet erst nach Ablauf
des vollen Intervalls nach der Initialisierung - die Werte wurden ja
waehrend der Init-Sequenz gerade erst frisch abgefragt.

| Option | Standard | Bedeutung |
|---|---|---|
| `bat_status_interval_s` | 3600 (60 min) | `Bat.GetStatus` |
| `es_mode_interval_s` | 900 (15 min) | `ES.GetMode` |
| `es_status_interval_s` | 300 (5 min) | `ES.GetStatus` |

### Abschnitt "Passiv Mode Settings"

| Option | Standard | Bedeutung |
|---|---|---|
| `power` | 800 | **Deckel** fuer die maximale Entlade-/Einspeiseleistung des automatischen Reglers und Sollwert beim manuellen Umschalten auf "Passive" im HA-Dropdown. Zur Laufzeit ueber die HA-Entity `number.passive_default_power` aenderbar (z.B. SOC-abhaengig per Automatisierung absenken, damit der Akku nicht zu schnell entladen wird). 0 ist gueltig und sperrt das Entladen im Passive-Mode vollstaendig. |
| `cd_time` | 60 | Nachlaufzeit/Countdown (Sekunden), die mit jedem Passive-Kommando mitgesendet wird. Ebenfalls live ueber `number.passive_cd_time` aenderbar. |
| `max_cd_time` | 3600 | Obergrenze fuer `cd_time` (Geraetevorgabe: max. 3600s = 60 min) |

**Wichtig:** `power` begrenzt nur die **Entlade-/Einspeiserichtung**
(positive Werte). Die Laderichtung (negative Werte, Laden aus dem Netz)
wird ausschliesslich durch `min_output_w` (Abschnitt "Selfconsumption
Control Parameters for Passive Mode") begrenzt.

### Abschnitt "Selfconsumption Control Parameters for Passive Mode"

Traeger Filter (Totzone, Slew-Rate, Mindeständerung, Hold-off), der aus
einer externen Leistungsmessung (`shelly_power_topic`) einen ruhigen
Passive-Mode-Sollwert fuer den Marstek berechnet, statt auf jede Spitze zu
reagieren.

| Option | Standard | Bedeutung |
|---|---|---|
| `deadzone_w` | 40 | Rauschunterdrueckung, W |
| `min_setpoint_change_w` | 50 | Mindeständerung vor erneutem Senden, W |
| `max_step_w` | 125 | Max. Leistungsschritt pro Zyklus, W |
| `min_output_w` | -1500 | Harte Ladegrenze (negativ = Laden). Vom `power`-Deckel NICHT beeinflussbar. |
| `max_output_w` | 800 | Harte Einspeisegrenze - der Marstek koennte physisch mehr, das ist eine bewusste Zusatzbegrenzung. Der `power`-Deckel (siehe "Passiv Mode Settings") kann diese Grenze nur verschaerfen, nie ueberschreiten. |
| `min_send_interval_s` | 30 | Mindestabstand zwischen zwei Sendungen, 0-60s (0 = kein Mindestabstand) |
| `shelly_power_topic` | (leer) | MQTT-Topic einer externen Leistungsmessung (z.B. Shelly EM), treibt den automatischen Regler an. Leer = Regler deaktiviert, nur manuelle Passive-Steuerung ueber HA moeglich |
| `shelly_debounce_time_s` | 10.0 | Mittelungsfenster (Sekunden, 0-300, 0 = deaktiviert) fuer den rohen Leistungswert, BEVOR er in die Regellogik einfliesst - glaettet kurze Ausreisser/Rauschen. Zur Laufzeit ueber `number.shelly_debounce_time_s` aenderbar; wird beim (erneuten) manuellen Wechsel in den Passive-Mode automatisch zurueckgesetzt (frische Mittelung, alte Samples verworfen). |

### Abschnitt "Message Settings"

| Option | Standard | Bedeutung |
|---|---|---|
| `max_retry` | 3 | Max. Wiederholungen im laufenden Betrieb (0-10), danach `communication_fail` |
| `timeout_s` | 1.0 | Timeout pro Versuch im laufenden Betrieb |
| `min_inter_message_delay_s` | 2.0 | Mindestabstand zwischen zwei GESENDETEN Nachrichten, egal welcher Art (0-30s, 0 = deaktiviert). Verhindert, dass mehrere unabhaengige Status-Abfragen (Bat/ES.GetMode/ES.GetStatus) zufaellig praktisch gleichzeitig beim Geraet landen. **Control-Kommandos werden davon nie aufgehalten** - sie duerfen sich jederzeit sofort dazwischen einreihen, dieser Wert bremst ausschliesslich aufeinanderfolgende Status-Abfragen. |

### Abschnitt "Init Settings"

| Option | Standard | Bedeutung |
|---|---|---|
| `base_timeout_s` | 2.0 | Timeout des ersten Versuchs waehrend der Erstinitialisierung |
| `timeout_increment_s` | 10.0 | Steigerung des Timeouts pro weiterem Versuch |
| `max_retries` | 4 | Max. Wiederholungen waehrend der Init-Sequenz, danach `communication_fail` |
| `inter_command_delay_s` | 10.0 | Pause zwischen den einzelnen Init-Kommandos (`Marstek.GetDevice`, `Wifi.GetStatus`, ..., `Led.Ctrl`), damit das Geraet beim Start nicht mit neun Anfragen praktisch gleichzeitig belastet wird |

### Abschnitt "DOD (Depth of Discharge)"

| Option | Standard | Bedeutung |
|---|---|---|
| `startup_value` | 88 | DOD-Wert (30-88), der bei jedem Add-on-Start gesetzt wird |

### Abschnitt "Additional Settings"

| Option | Standard | Bedeutung |
|---|---|---|
| `led_startup_state` | 0 | Panel-LED beim Start: 0 = aus, 1 = an |
| `ble_block_startup_enable` | 0 | Bluetooth-Advertising beim Start: 0 = aktiv, 1 = deaktiviert |

### Abschnitt "Logging"

| Option | Standard | Bedeutung |
|---|---|---|
| `log_level` | `info` | `debug` faerbt zusaetzlich SEND/RECV/TIMEOUT-Zeilen ein (siehe [Logging](#logging)) |

## Logging

Über die Option `log_level` (`debug`/`info`/`warning`/`error`) steuerbar.
Bei `debug` werden zusätzlich alle SEND/RECV/TIMEOUT-Zeilen der
UDP-Kommunikation farblich unterschieden ausgegeben: **gelb** = Control-
Kommandos (ES.SetMode, DOD.SET, Ble.Adv, Led.Ctrl), **cyan** = Status-
Abfragen (Bat.GetStatus, ES.GetStatus, ES.GetMode), **magenta** = Init-
Sequenz, **rot** = Verbindungsstatus-Meldungen (Communication
established/Fail). Per Screenshot bestätigt funktionsfähig im
HA-Add-on-Log-Tab. Zusätzlich stehen die Text-Tags
`[STATUS]`/`[CONTROL]`/`[INIT]` direkt in der Nachricht selbst, falls eine
Log-Ansicht (z. B. `docker logs` ohne TTY) ANSI-Codes einmal nicht
rendert.

### Debugging - ControlLogic (eigener Schalter, eigene Farbe)

Die Option `debug_control_logic` (Standard: aus) aktiviert einen **separat
vom allgemeinen `log_level` schaltbaren** Debug-Log-Kanal (**grün**) nur
für die Interna der Passive-Mode-Regelschleife:

- Shelly-Eingang (roh)
- Shelly-Eingang (entprellt/gemittelt, inkl. Fenstergröße und Sample-Anzahl)
- Errechnete Leistungsänderung pro Zyklus (aktueller Sollwert → Ziel)

Bewusst getrennt von `log_level`, weil bei häufigen Shelly-Updates (z. B.
alle 1-5s) hier sehr viele Zeilen anfallen können - das würde bei
`log_level=debug` sonst das restliche Log fluten. Diese Option lässt sich
unabhängig aktivieren, auch wenn `log_level` auf `info` steht.

**Automatische Aktivierung beim Wechsel in den Passive-Mode:** Unabhängig
von dieser Konfigurationsoption schaltet die Bridge den ControlLogic-Logger
automatisch auf DEBUG, sobald `select.energy_mode` manuell auf "Passive"
gestellt wird - inklusive einer sofortigen Diagnosemeldung, ob überhaupt
ein `shelly_power_topic` konfiguriert ist. Damit lässt sich auf einen
Blick prüfen, ob der Shelly-Eingang überhaupt ankommt, ohne vorher die
Konfiguration ändern und das Add-on neu starten zu müssen. Beim
Verlassen des Passive-Mode (Wechsel zu Auto/AI/Ups) kehrt der Logger zum
konfigurierten Grundzustand zurück (an, falls `debug_control_logic`
aktiviert ist, sonst aus).

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

Wichtige Control-Entities fuer den Passive-Mode:

- `number.passive_default_power` - der Entlade-Deckel (siehe "Passiv Mode
  Settings" oben). Fuer eine SOC-abhaengige Automatisierung z.B. an
  `sensor.battery_soc` koppeln: hoher SOC -> Deckel hoch (bis max.
  `max_output_w`), niedriger SOC -> Deckel absenken.
- `number.passive_cd_time` - die Nachlaufzeit/Countdown fuer Passive-Kommandos.
- `number.shelly_debounce_time_s` - Mittelungsfenster fuer die Entprellung des externen Leistungssignals.
- `select.energy_mode` - Auto/AI/Passive/Ups (Manual wird bewusst nicht
  unterstuetzt).
- `button.passive_resend` - **"Resend Passive Command"**: sendet das
  aktuelle Passive-Kommando (Deckel/cd_time) erneut, ohne den Modus zu
  wechseln. Home-Assistant-`select`-Entities loesen beim erneuten Klicken
  auf den bereits aktiven Wert oft KEINE neue MQTT-Nachricht aus (das
  Frontend erkennt keine Zustandsaenderung) - dieser Button umgeht das
  zuverlaessig, da Button-Entities bei jedem Druck garantiert senden.
  Setzt dabei auch den Countdown ("Passive Countdown Remaining") zurueck
  und aktiviert automatisch das ControlLogic-Debugging (siehe oben).

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
- Die gruppierten Konfigurationsabschnitte setzen HA Supervisor >= 2025.10
  voraus. Auf aelteren Installationen ist das Rendering der Abschnitte
  nicht getestet.
- Der Passive-Regler sendet jetzt automatisch ein "Keepalive"-Kommando
  (unveraenderter Sollwert) kurz bevor die geraeteseitige `cd_time`
  ablaeuft, damit der Passive-Sollwert nicht verloren geht, wenn sich
  ueber laengere Zeit kein echtes Update ergibt. Dieses Verhalten ist
  nicht abschaltbar.
