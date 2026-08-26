# Changelog

## 0.0.5

- `log_level` an das Ende der Konfiguration verschoben (war vorher unter
  "Allgemein" ganz oben).
- MQTT-Override-Felder (`mqtt_host`, `mqtt_username`, `mqtt_password`) aus
  dem generischen "Nicht verwendete optionale Konfigurationsoptionen"-
  Bereich in eine echte, benannte Gruppe **"MQTT Settings"** verschoben
  (gleiche Darstellung wie die anderen Abschnitte).
  `mqtt_port` bleibt bewusst ausserhalb dieser Gruppe ohne Default-Wert,
  da ein vorbelegter Port die "leer lassen = automatische Erkennung via
  Mosquitto-Service-Discovery"-Logik verhindern wuerde. Dadurch kann sich
  `mqtt_port` in der UI ggf. weiterhin anders verhalten als die drei
  anderen Felder der Gruppe - ohne echten Supervisor nicht verifizierbar.
- `addon_options.py` entsprechend angepasst (liest `mqtt_host`/
  `mqtt_username`/`mqtt_password` jetzt aus der `mqtt_settings`-Gruppe,
  `mqtt_port` weiterhin top-level).

## 0.0.4

- **Strukturelle Änderung**: Add-on-Optionen von flachen, prefix-benannten
  Schlüsseln (`message_settings_max_retry`, `controller_deadzone_w`, ...)
  auf echte verschachtelte Gruppen umgestellt (`message_settings.max_retry`,
  `selfconsumption_control.deadzone_w`, ...). Seit HA Supervisor 2025.10
  werden diese Gruppen als eigene Unterformulare mit Titel in der
  Add-on-Konfigurations-UI angezeigt: "Scanrate for Statuscalls", "Passiv
  Mode Settings", "Selfconsumption Control Parameters for Passive Mode",
  "Message Settings", "Init Settings", "DOD (Depth of Discharge)",
  "Additional Settings".
- `addon_options.py` entsprechend umgebaut (liest jetzt verschachtelte
  Gruppen statt flacher Praefix-Schluessel), inkl. Fallback auf Defaults
  falls eine ganze Gruppe im `options.json` fehlt.
- `translations/en.yaml` um Gruppennamen/-beschreibungen sowie
  Feldbeschreibungen fuer alle Optionen ergaenzt.
- Voraussetzung ergaenzt: HA Supervisor >= 2025.10 fuer korrekte Anzeige
  der gruppierten Abschnitte.

## 0.0.3

- Gliedernde Überschriften in `config.yaml` ergänzt (sowohl im `options:`-
  als auch im `schema:`-Block), damit die Struktur beim Lesen der Datei
  leichter erkennbar ist: "Scanrate for Statuscalls", "Passiv Mode
  Settings", "Selfconsumption Control Parameters for Passive Mode",
  "Message Settings", "Init Settings", "DOD (Depth of Discharge)",
  "Additional Settings". Rein kosmetisch (YAML-Kommentare) - keine
  funktionale Änderung, HA zeigt diese Überschriften nicht in der
  Add-on-Konfigurations-UI an, nur beim Lesen der Rohdatei.

## 0.0.2

- Erklaerende Kommentare zu `passive_power` in `config.yaml` ergaenzt
  (Options- und Schema-Block): deckelt die maximale Einspeise-/Entladeleistung
  des Reglers, selbst wenn rechnerisch mehr ermittelt wuerde; Laderichtung
  bleibt davon unberuehrt und wird weiterhin nur durch `controller_min_output_w`
  begrenzt.

## 0.0.1

- Erste Version: UDP-Client mit Prioritaets-Queue (Control vor Status),
  Init-Sequenz, HA-MQTT-Discovery fuer alle Marstek-Datenpunkte, traeger
  Passive-Mode-Regler mit optionaler externer Leistungsmessung (z.B. Shelly),
  Communication-Fail/System-Ready-Status fuer Automatisierungen.
- Fix: ungueltiges `watchdog: null` Feld aus config.yaml entfernt (verhinderte
  das Laden des Add-ons durch den Supervisor).
- icon.png/logo.png ergaenzt.
- Farbcodiertes Debug-Logging (Control/Status/Init/Verbindungsstatus) in
  main.py aktiviert.
- Erster Poll-Zyklus nach der Initialisierung wartet jetzt das volle
  konfigurierte Intervall ab, statt sofort erneut abzufragen.
- Default fuer `init_inter_command_delay_s` von 5s auf 10s erhoeht.
- `init_timeout_increment_s` Default von 5s auf 10s erhoeht.
- `passive_power` umgebaut: von einem statischen, wirkungslosen Startwert zu
  einem zur Laufzeit ueber HA (`number.passive_default_power`) aenderbaren
  Deckel fuer die maximale Entlade-/Einspeiseleistung des automatischen
  Passive-Mode-Reglers (z.B. fuer SOC-abhaengige Automatisierungen). Wirkt
  nur auf die Entlade-/Einspeiserichtung, nicht auf das Laden.
  Default von 50 auf 800 (= `controller_max_output_w`) geaendert, damit
  ohne aktive Automatisierung keine zusaetzliche Einschraenkung entsteht.
  `passive_cd_time` (`number.passive_cd_time`) ist aus demselben Grund jetzt
  ebenfalls tatsaechlich live wirksam (vorher nur kosmetisch).
- Config-Optionen neu gegliedert (Allgemein, Abtastrate, Passive-Mode,
  Regelungsparameter, Message Settings, Initphase, DOD, Sonstige, MQTT-
  Override) und `message_max_retry`/`message_timeout_s` in
  `message_settings_max_retry`/`message_settings_timeout_s` umbenannt.
