# Changelog

## 0.0.10

- **Neuer Debug-Kanal "Debugging - ControlLogic"** (`logging_settings.debug_control_logic`,
  Standard: aus), eigene Farbe (grün), separat vom allgemeinen `log_level`
  schaltbar. Loggt: Shelly-Eingang (roh), Shelly-Eingang (entprellt, inkl.
  Fenstergroesse/Sample-Anzahl), errechnete Leistungsaenderung pro Zyklus.
  Bewusst getrennt vom allgemeinen Log-Level, da bei haeufigen Shelly-
  Updates hier sehr viele Zeilen anfallen koennen.

## 0.0.9

- **cd_time-Keepalive**: Der Passive-Regler sendet jetzt automatisch ein
  Kommando mit unveraendertem Sollwert kurz bevor die geraeteseitige
  `cd_time` ablaeuft (Schwelle: 20% Marge, mind. 5s), selbst wenn Totzone/
  Mindeständerung sonst kein Update verlangen wuerden. Ohne dieses
  Verhalten haette das Geraet nach Ablauf von `cd_time` ohne neues
  Kommando seinen Passive-Sollwert verloren. Bypasst dafuer auch den
  Hold-off-Timer (`min_send_interval_s`).
- **Eingangsentprellung fuer `shelly_power_topic`** (neues Modul
  `input_averager.py`): der rohe externe Leistungsmesswert wird jetzt vor
  der Regellogik ueber ein konfigurierbares Zeitfenster gemittelt
  (`shelly_debounce_time_s`, Standard 10s, 0 = aus). Neue Option in der
  Gruppe "Selfconsumption Control Parameters for Passive Mode", neue
  HA-Entity `number.shelly_debounce_time_s` (live aenderbar), wird beim
  manuellen (Re-)Aktivieren des Passive-Mode automatisch zurueckgesetzt.
- **Neuer konfigurierbarer Mindestabstand zwischen Nachrichten**
  (`message_settings.min_inter_message_delay_s`, Standard 2.0s): verhindert,
  dass mehrere unabhaengige Status-Poll-Loops (Bat.GetStatus, ES.GetMode,
  ES.GetStatus) zufaellig praktisch gleichzeitig beim Geraet ankommen
  (in der Praxis beobachtet: 1ms Abstand zwischen aufeinanderfolgenden
  Abfragen). **Control-Kommandos werden davon nie aufgehalten** - sie
  duerfen sich laut Test jederzeit sofort dazwischen einreihen, auch
  waehrend eine Status-Abfrage auf den Mindestabstand wartet.
- **Korrektur einer Fehleinschaetzung**: Ein per Screenshot bestaetigter
  Test zeigt, dass ANSI-Farbcodierung im HA-Add-on-Log-Tab tatsaechlich
  funktioniert. Die vorherige Annahme, ANSI wuerde herausgefiltert,
  basierte auf per Copy-Paste kopiertem Logtext - Copy-Paste aus einer
  farbig gerenderten Weboberflaeche kann grundsaetzlich nie Farbe
  mitkopieren. Die zwischenzeitlich entfernte ANSI-Faerbung ist wieder
  aktiviert.

## 0.0.8

- ANSI-Farbcodes von "hellen" SGR-Codes (90-97) auf **Standard-SGR-Codes
  (30-37)** umgestellt, da manche ANSI->HTML-Konverter (u.a. offenbar
  Teile des HA-Add-on-Log-Viewers) die erweiterten "bright"-Codes nicht
  erkennen und dadurch weder Farbe noch lesbaren Text anzeigen.
  **Hinweis:** Ob der Add-on-Log-Tab in deiner HA-Version ANSI-Codes
  tatsaechlich in Farbe umwandelt oder als rohe Escape-Sequenzen anzeigt,
  konnte ohne echten Supervisor nicht abschliessend verifiziert werden -
  bitte nach dem Update pruefen. Falls weiterhin keine Farbe zu sehen ist,
  sind die reinen Text-Tags `[STATUS]`/`[CONTROL]`/`[INIT]`/`[COMM]` in
  jeder Log-Zeile bereits unabhaengig von ANSI-Unterstuetzung vorhanden.

## 0.0.7

- `log_level` in eine eigene, benannte Gruppe **"Logging"** verschoben -
  gleiche aufklappbare Darstellung wie alle anderen Abschnitte, statt als
  einzelnes, ungruppiertes Feld am Ende.
- `main.py` entsprechend angepasst (liest `log_level` jetzt aus der
  `logging_settings`-Gruppe).

## 0.0.6

- Letzte verbleibende ungruppierte Basis-Felder (`device_ip`,
  `device_udp_port`, `device_ble_mac`, `device_type`,
  `mqtt_discovery_prefix`, `mqtt_base_topic`, `mqtt_suggested_area`) in
  eine echte, benannte Gruppe **"Marstek Device"** verschoben - gleiche
  Darstellung wie alle anderen Abschnitte. Damit sind jetzt saemtliche
  Optionen ausser `log_level` und `mqtt_port` gruppiert.
- `addon_options.py` entsprechend angepasst (liest Geraete-/Verbindungswerte
  jetzt aus der `marstek_device`-Gruppe).

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
